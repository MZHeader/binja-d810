"""ILAdapter for MLIL: the v1 pass behind the engine's IL-independent contract."""

from __future__ import annotations

from typing import Hashable, Iterator, Optional

from binaryninja import (
    ILSourceLocation, MediumLevelILFunction, MediumLevelILInstruction, MediumLevelILOperation as Op, RegisterValueType,
    SSAVariable,
)

from ...deadline import Deadline
from ...ir.expr import Expr, is_candidate, tree_size
from ...ir.paths import Merge, path_variants
from ..adapter import Candidate, Skip
from ..deadcode import dead_definitions, dying_with_root, shown_in
from .lift import DIVISION_OPS, SUPPORTED_OPS, candidate_roots, is_safe_division, lift, lift_value
from .lower import lower

_ARITH = frozenset({Op.MLIL_ADD, Op.MLIL_SUB, Op.MLIL_MUL, Op.MLIL_NEG, Op.MLIL_LSL})
_BITWISE = frozenset({Op.MLIL_AND, Op.MLIL_OR, Op.MLIL_XOR, Op.MLIL_NOT})
_COMPARES = frozenset({Op.MLIL_CMP_E, Op.MLIL_CMP_NE, Op.MLIL_CMP_ULT, Op.MLIL_CMP_ULE, Op.MLIL_CMP_UGT,
                       Op.MLIL_CMP_UGE, Op.MLIL_CMP_SLT, Op.MLIL_CMP_SLE, Op.MLIL_CMP_SGT, Op.MLIL_CMP_SGE})
_SHARED_FACTOR = 4  # distinct nodes allowed for a shared-value lift, as a multiple of maxNodes
_PURE_LEAVES = frozenset({Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD, Op.MLIL_CONST, Op.MLIL_CONST_PTR})
_COPIES = frozenset({Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD})
# Flags a rewrite may leave dead (cond:0 = t != 0 once t's 0/1 value is inlined): pure, never trap.
_PURE_FLAGS = frozenset({Op.MLIL_CMP_E, Op.MLIL_CMP_NE, Op.MLIL_BOOL_TO_INT})


class MLILAdapter:
    level = "mlil"

    def __init__(self, mlil: MediumLevelILFunction, max_nodes: int = 200, deadline: Deadline = Deadline.never()):
        self.mlil = mlil
        self.max_nodes = max_nodes
        # Enumeration lifts every root; in a large function with no MBA that is all the work, and
        # the engine only sees candidates it is given (snagit.dll: 60 s, Binary Ninja gave up).
        self.deadline = deadline
        self.backups: list[tuple[int, int]] = []

    def has_candidates(self) -> bool:
        seen = set()
        for instr in self.mlil.instructions:
            if self.deadline.expired():
                return False
            rejected: list = []
            for root in candidate_roots(instr, rejected, divisions=False):  # operations seen either way
                seen.update(_operations(root))
            for expr, _ in rejected:
                seen.update(_operations(expr))
        # MBA, or a comparison over a bitwise operation or a product (opaque predicates)
        return (bool(seen & _ARITH) and bool(seen & _BITWISE)) or (
            bool(seen & _COMPARES) and (bool(seen & _BITWISE) or Op.MLIL_MUL in seen))

    def candidates(self, skip: Skip) -> Iterator[Candidate]:
        if self.mlil.ssa_form is None:
            self.mlil.generate_ssa_form()
        definitions: dict = {}  # SSA definitions, shared by every lift of this SSA form
        for instr in reversed(list(self.mlil.instructions)):
            if self.deadline.expired():
                return
            rejected: list = []
            roots = list(candidate_roots(instr, rejected))
            for expr, reason in rejected:
                seen = lift(expr.ssa_form, root_instr=instr, max_nodes=self.max_nodes, effect_leaves=True,
                            definitions=definitions)
                if seen is not None and is_candidate(seen):
                    skip(expr.address, reason)
            for root in reversed(roots):
                inlined: dict = {}
                stats: dict = {}
                ir = lift(root.ssa_form, root_instr=instr, max_nodes=self.max_nodes, inlined=inlined,
                          definitions=definitions, stats=stats)
                shared = None
                if stats.get("budget_hit"):
                    # Too large as a tree (v3.1): values read several times, as in an opaque
                    # predicate's masked-constant chain, counted once. Only a proven constant may
                    # come from it, so versions are not checked (as the unchecked lift below).
                    dag = lift(root.ssa_form, root_instr=instr, check_versions=False, shared_once=True,
                               max_nodes=_SHARED_FACTOR * self.max_nodes, definitions=definitions)
                    if dag is not None and tree_size(dag) > self.max_nodes and is_candidate(dag):
                        shared = dag
                unchecked = through = None
                unchecked_inlined: dict = {}
                if (root.operation in _COMPARES or ir is None) and shared is None:
                    # A condition, or a root the version-safe lift could not see at all: a proven
                    # constant reads no variable, so it may come from a lift through inputs whose
                    # registers were reused before the root (x86-64 -O2). Not values the checked lift
                    # saw partly (stale inputs): folding two such values in mba_linear_030 (x86-64
                    # -O2) made Binary Ninja's HLIL drop the result's definition. Not when the lift
                    # over shared values exists: a tree lift as large stops at the same size (v3.3).
                    unchecked = through = lift(root.ssa_form, root_instr=instr, check_versions=False,
                                               max_nodes=self.max_nodes, inlined=unchecked_inlined,
                                               definitions=definitions)
                    if unchecked is not None and not is_candidate(unchecked):
                        unchecked = None
                constant_only = (ir is None or not is_candidate(ir)) and unchecked is not None
                if shared is not None and (ir is None or not is_candidate(ir)) and unchecked is None:
                    ir, inlined, constant_only = shared, {}, True
                elif constant_only:
                    ir, inlined = unchecked, unchecked_inlined
                paths = None
                if root.operation in _COMPARES and (ir is None or not is_candidate(ir)) and unchecked is None:
                    # A condition over merged values whose per-path variants are candidates (k != 0 with
                    # k = phi((x - 1) * x & 1, (x + x * x) & 1)): constant-only, from its paths.
                    alternatives = None if through is None else self._path_variants(through, instr)
                    if alternatives and any(is_candidate(v) for variants in alternatives for v in variants):
                        ir, inlined, constant_only, paths = through, {}, True, tuple(map(tuple, alternatives))
                if ir is not None and (is_candidate(ir) or paths is not None):
                    defines, single = self._defines(instr, root)
                    nodes, mixed, dying = self._visible(instr, root, inlined)
                    yield Candidate(root, root.address, ir, defines, single, frozenset(inlined), nodes, mixed,
                                    instr, frozenset(inlined) - dying, unchecked, constant_only, shared,
                                    self._consumer_kinds(instr, root), self.root_kinds, paths)

    def shallow(self, candidate: Candidate, keep: Optional[frozenset] = None) -> Optional[Candidate]:
        """The root lifted with `keep` (default: the definitions that stay alive) as leaves where
        they are live (ILAdapter.shallow)."""
        keep = candidate.shared if keep is None else keep
        if not keep or candidate.context is None:
            return None
        instr, root = candidate.context, candidate.root
        inlined: dict = {}
        ir = lift(root.ssa_form, root_instr=instr, max_nodes=self.max_nodes, inlined=inlined, keep=keep)
        if ir is None:  # bitwise-only is fine: Tier 1 rules simplify it (a ^ 0xff00ff)
            return None
        defines, single = self._defines(instr, root)
        nodes, mixed, _ = self._visible(instr, root, inlined)
        # The same statement as the candidate's: the same gate (v3.1 statement kinds).
        return Candidate(root, root.address, ir, defines, single, frozenset(inlined), nodes, mixed, instr,
                         consumer_kinds=self._consumer_kinds(instr, root), root_kinds=self.root_kinds)

    def path_variants(self, candidate: Candidate) -> Optional[list[list[Expr]]]:
        """The candidate's condition once per incoming path of the merged values it reads (spec v3.2
        goal 3), from its lift through every input (versions unchecked: only a proven constant may
        come from it), as alternatives: invariant merges alone, then every merge split (v3.3).
        Conditions only; None when it reads no merged value."""
        if candidate.context is None or candidate.root.operation not in _COMPARES:
            return None
        base = candidate.unchecked if candidate.unchecked is not None else candidate.ir
        return self._path_variants(base, candidate.context)

    def _path_variants(self, base: Expr, instr: MediumLevelILInstruction) -> Optional[list[Expr]]:
        merges: dict = {}
        definitions: dict = {}

        def merge_of(leaf) -> Optional[Merge]:
            if not isinstance(leaf.ref, SSAVariable):
                return None
            key = (leaf.ref.var.identifier, leaf.ref.version, leaf.width)
            if key not in merges:
                merges[key] = self._merge(leaf.ref, leaf.width, instr, definitions)
            return merges[key]

        alternatives = []
        for invariant_only in (True, False):
            variants = path_variants(base, merge_of, invariant_only=invariant_only)
            if variants and variants not in alternatives:
                alternatives.append(variants)
        return alternatives or None

    def _merge(self, ssa_var: SSAVariable, width: int, instr: MediumLevelILInstruction,
               definitions: dict) -> Optional[Merge]:
        """A phi's block and its incoming value along each of the block's incoming edges: the version
        of the variable at the end of the edge's source block, which must be one of the phi's sources."""
        ssa = self.mlil.ssa_form
        phi = ssa.get_ssa_var_definition(ssa_var)
        if phi is None or phi.operation != Op.MLIL_VAR_PHI:
            return None
        source = _invariant_source(ssa, ssa_var)
        if source is not None:
            # Every value of the merge is some instance of `source` (v3.3): one incoming value, which
            # path_variants renames apart like any other.
            return Merge(f"invariant:{ssa_var.var.identifier}:{ssa_var.version}",
                         (lift_value(ssa, source, width, root_instr=instr, max_nodes=self.max_nodes,
                                     definitions=definitions),))
        sources = {(s.var.identifier, s.version) for s in phi.src}
        block = phi.il_basic_block
        incoming = []
        for edge in block.incoming_edges:
            reaching = _reaching(ssa, edge.source, ssa_var.var)
            if reaching is None or (reaching.var.identifier, reaching.version) not in sources:
                return None
            incoming.append(lift_value(ssa, reaching, width, root_instr=instr, max_nodes=self.max_nodes,
                                       definitions=definitions))
        return Merge(block.index, tuple(incoming))

    def _visible(self, instr: MediumLevelILInstruction, root: MediumLevelILInstruction, inlined: dict):
        """Size and mix of what the user sees: the root plus the definitions that die with it."""
        ssa = self.mlil.ssa_form
        definitions = {key: ssa.get_ssa_var_definition(var) for key, var in inlined.items()}
        definitions = {key: d for key, d in definitions.items() if d is not None and hasattr(d, "src")}
        uses = {key: [u.instr_index for u in ssa.get_ssa_var_uses(inlined[key])] for key in definitions}
        dying = dying_with_root(uses, {key: d.instr_index for key, d in definitions.items()},
                                instr.ssa_form.instr_index)
        # Copies (rax_6 = var_24 at -O0) are not counted: HLIL propagates them away, so a rewrite
        # that only removes copies while duplicating a kept value's definition grows the code shown.
        trees = [root] + [definitions[key].src for key in dying if definitions[key].src.operation not in _COPIES]
        # Mixed if one statement as HLIL shows it is. Single-use temporaries fold into their use
        # (mba_challenge: t = a + 3; u = t | b reads as one mixed line); a value used several times
        # is a statement of its own (snagit.dll: a = x * 7 + y read twice by (a & M) | (~a & ~M) —
        # each unmixed, and merging them is what the gate refuses).
        statements: dict = {None: set(_operations(root))}
        shown = shown_in(dying, uses, {key: d.instr_index for key, d in definitions.items()},
                         instr.ssa_form.instr_index)
        for key in dying:
            statements.setdefault(shown[key], set()).update(_operations(definitions[key].src))
        mixed = any(ops & _ARITH and ops & _BITWISE for ops in statements.values())
        self.root_kinds = _kinds(statements[None])
        return sum(_size(tree) for tree in trees), mixed, dying

    def _consumer_kinds(self, instr: MediumLevelILInstruction, root: MediumLevelILInstruction) -> Optional[frozenset]:
        """The kinds of the statements the root's value flows into through single uses, as HLIL
        merges them into one statement (v3.1); None if the root is not a whole definition."""
        if instr.operation != Op.MLIL_SET_VAR or instr.src.expr_index != root.expr_index:
            return None
        ssa = self.mlil.ssa_form
        value, kinds = instr.ssa_form.dest, set()
        for _ in range(16):
            uses = ssa.get_ssa_var_uses(value)
            if len(uses) != 1 or uses[0].operation == Op.MLIL_VAR_PHI:
                break
            use = uses[0]
            tree = use.src if use.operation == Op.MLIL_SET_VAR_SSA else use
            kinds |= _kinds(set(_operations(tree)))
            if use.operation != Op.MLIL_SET_VAR_SSA or not _pure(use.src):
                break
            value = use.dest
        return frozenset(kinds)

    def _defines(self, instr: MediumLevelILInstruction, root: MediumLevelILInstruction):
        """The SSA value the root is the whole definition of, and whether it has one use."""
        if instr.operation != Op.MLIL_SET_VAR or instr.src.expr_index != root.expr_index:
            return None, False
        dest = instr.ssa_form.dest
        return _key(dest), len(self.mlil.ssa_form.get_ssa_var_uses(dest)) == 1

    def replace(self, candidate: Candidate, replacement: Expr) -> None:
        root = candidate.root
        loc = ILSourceLocation.from_instruction(root)
        self.backups.append((root.expr_index, root.copy_to(self.mlil)))
        self.mlil.replace_expr(root.expr_index, lower(self.mlil, replacement, loc))

    def propagate_constants(self, rewritten: list[Candidate]) -> int:
        """After a pass (SSA regenerated): reads of a value a rewritten instruction defines, when
        Binary Ninja's dataflow now knows it is a constant, read the constant (v3.1). Covers partial
        writes too (xor r14d, r14d; setne r14b folded: r14 is 1). Exact for the SSA value; phi nodes
        keep their sources (the dataflow carries constants through them). Returns the reads replaced;
        the definitions then die and are removed as newly dead."""
        ssa = self.mlil.ssa_form
        replaced = 0
        for candidate in rewritten:
            if candidate.context is None:
                continue
            instr = self.mlil[candidate.context.instr_index].ssa_form
            if instr.operation not in (Op.MLIL_SET_VAR_SSA, Op.MLIL_SET_VAR_SSA_FIELD):
                continue
            dest = instr.dest
            value = ssa.get_ssa_var_value(dest)
            if value.type != RegisterValueType.ConstantValue:
                continue
            for use in ssa.get_ssa_var_uses(dest):
                if use.operation == Op.MLIL_VAR_PHI:
                    continue
                # The use's own non-SSA instruction: SSA form numbers instructions differently (phi).
                for node, offset in _var_reads(use.non_ssa_form, dest.var):
                    bits = 8 * node.size or 1  # a flag's value is one bit (if (true), spike S2)
                    constant = (value.value >> (8 * offset)) & ((1 << bits) - 1)
                    self.backups.append((node.expr_index, node.copy_to(self.mlil)))
                    self.mlil.replace_expr(node.expr_index, self.mlil.const(node.size, constant,
                                                                            ILSourceLocation.from_instruction(node)))
                    replaced += 1
        return replaced

    def finish(self) -> None:
        self.mlil.finalize()
        self.mlil.generate_ssa_form()

    def dead_assignments(self) -> set[Hashable]:
        """Instruction indices of pure SET_VARs whose value is never used, even indirectly.

        At -O0 a rewritten result leaves chains of copies through stack slots behind
        (x8_1 = x8 ^ x9; var_10 = x8_1; x8_2 = var_10; ...), which HLIL would still show.
        """
        pure, roots, index_of = {}, [], {}
        for instr in self.mlil.ssa_form.instructions:
            if instr.operation == Op.MLIL_SET_VAR_SSA and _pure(instr.src):
                key = _key(instr.dest)
                pure[key] = [_key(v) for v in _ssa_vars(instr.src)]
                index_of[key] = instr.non_ssa_form.instr_index
            elif instr.operation == Op.MLIL_SET_VAR_SSA_FIELD and _pure(instr.src):
                # A partial write (var_8:5.b = x ^ y at -O0) reads the previous version as well.
                key = _key(instr.dest)
                pure[key] = [_key(instr.prev)] + [_key(v) for v in _ssa_vars(instr.src)]
                index_of[key] = instr.non_ssa_form.instr_index
            elif instr.operation == Op.MLIL_VAR_PHI:
                # A merge is not a use: its sources live only if its result does (a folded
                # predicate's per-path values, merged at -O0 through a stack slot). Not removable.
                pure[_key(instr.dest)] = [_key(v) for v in instr.src]
            else:
                roots.extend(_key(v) for v in _ssa_vars(instr))
        return {index_of[key] for key in dead_definitions(pure, roots) if key in index_of}

    def remove(self, dead: set[Hashable]) -> None:
        for index in sorted(dead):
            instr = self.mlil[index]
            self.backups.append((instr.expr_index, instr.copy_to(self.mlil)))
            self.mlil.replace_expr(instr.expr_index, self.mlil.nop(ILSourceLocation.from_instruction(instr)))

    def cfg_shape(self) -> tuple:
        return tuple((bb.start, bb.end, self.mlil[bb.end - 1].operation) for bb in self.mlil.basic_blocks)

    def restore(self) -> None:
        for expr_index, backup in reversed(self.backups):
            self.mlil.replace_expr(expr_index, backup)
        if self.backups:
            self.finish()
        self.backups.clear()


def _invariant_source(ssa, ssa_var: SSAVariable, limit: int = 64) -> Optional[SSAVariable]:
    """The one value entering a web of merges and whole copies from outside it, if there is exactly one
    (v3.3): i = phi(i0, i'), i' = esi, esi = phi(i0, i), as x-tunnel.bin carries y * y + 3 round a loop.
    Every member then holds some instance of that value. None otherwise."""
    web, outside, stack = set(), {}, [ssa_var]
    while stack:
        var = stack.pop()
        key = (var.var.identifier, var.version)
        if key in web or key in outside:
            continue
        definition = ssa.get_ssa_var_definition(var)
        if definition is not None and definition.operation == Op.MLIL_VAR_PHI:
            web.add(key)
            stack.extend(definition.src)
        elif (definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA
              and definition.src.operation == Op.MLIL_VAR_SSA and definition.src.size == definition.size):
            web.add(key)
            stack.append(definition.src.src)
        else:
            outside[key] = var
        if len(web) + len(outside) > limit or len(outside) > 1:
            return None
    return next(iter(outside.values())) if len(outside) == 1 else None


def _reaching(ssa, block, var) -> Optional[SSAVariable]:
    """The version of `var` leaving `block` (SSA form): written by its last instruction, or current there."""
    last = ssa[block.end - 1]
    for written in last.vars_written:
        if isinstance(written, SSAVariable) and written.var == var:
            return written
    try:
        return SSAVariable(var, last.non_ssa_form.get_ssa_var_version(var))
    except Exception:  # noqa: BLE001 - no non-SSA instruction for it
        return None


def _operations(e: MediumLevelILInstruction):
    stack = [e]
    while stack:
        node = stack.pop()
        yield node.operation
        stack.extend(op for op in node.operands if isinstance(op, MediumLevelILInstruction))


def _size(e: MediumLevelILInstruction) -> int:
    return sum(1 for _ in _operations(e))


def _pure(e: MediumLevelILInstruction) -> bool:
    if e.operation in _PURE_LEAVES:
        return True
    if e.operation in DIVISION_OPS:
        return is_safe_division(e, e)  # cannot trap: removable once dead (spec v3.2)
    if e.operation not in SUPPORTED_OPS and e.operation not in _PURE_FLAGS:
        return False
    return all(_pure(op) for op in e.operands if isinstance(op, MediumLevelILInstruction))


def _ssa_vars(e: MediumLevelILInstruction):
    """Every SSA variable in an expression tree. MLIL_RET's vars_read is empty, so walk operands.

    For instructions with side effects this also yields what they write (calls, phis), which only
    keeps more alive.
    """
    stack: list = [e]
    while stack:
        item = stack.pop()
        if isinstance(item, SSAVariable):
            yield item
        elif isinstance(item, MediumLevelILInstruction):
            stack.extend(item.operands)
        elif isinstance(item, list):
            stack.extend(item)


def _key(ssa_var: SSAVariable) -> tuple[int, int]:
    return ssa_var.var.identifier, ssa_var.version


def _var_reads(instr: MediumLevelILInstruction, var) -> list:
    """(node, byte offset) for each read of `var` in one non-SSA instruction: whole (MLIL_VAR) or a
    field (MLIL_VAR_FIELD, r14.d). Its destination is not a read."""
    found, stack = [], [instr]
    while stack:
        node = stack.pop()
        if node.operation == Op.MLIL_VAR and node.src == var:  # size 0: a flag (cond:0)
            found.append((node, 0))
            continue
        if node.operation == Op.MLIL_VAR_FIELD and node.src == var and node.size > 0:
            found.append((node, node.offset))
            continue
        for operand in node.operands:
            if isinstance(operand, MediumLevelILInstruction):
                stack.append(operand)
            elif isinstance(operand, list):
                stack.extend(o for o in operand if isinstance(o, MediumLevelILInstruction))
    return found


def _kinds(operations: set) -> frozenset:
    return frozenset(({"arith"} if operations & _ARITH else set()) | ({"bitwise"} if operations & _BITWISE else set()))
