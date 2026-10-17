"""MLIL (SSA) -> binja_d810 IR, following SSA definitions across instructions.

Version safety: every SSA variable left as a leaf must hold the version that is current at the
root instruction, because the rewrite is lowered there and reads variables by name. Binary Ninja
numbers SSA versions per storage location (x and x0 share register x0), so a variable counts as
overwritten when another variable in the same register is written. Intermediate definitions are
inlined, so their own reads need not be live; only the final leaves are checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from binaryninja import (
    MediumLevelILInstruction, MediumLevelILOperation as Op, RegisterValueType, SSAVariable, VariableSourceType,
)

from ...ir.expr import COUNT_OPS, BinOp, Const, Expr, UnOp, Var, variables
from ...ir.normalize import narrowed
from ...semantic.traps import never_traps
from ..effects import mlil_effect
from ..storage import (
    as_integer, contained, exact, field_read, fit, merge_partial_write, read_over_write, unary,
)

_BINARY = {Op.MLIL_ADD: "add", Op.MLIL_SUB: "sub", Op.MLIL_MUL: "mul", Op.MLIL_AND: "and",
           Op.MLIL_OR: "or", Op.MLIL_XOR: "xor", Op.MLIL_LSL: "shl", Op.MLIL_LSR: "lshr", Op.MLIL_ASR: "ashr",
           Op.MLIL_ROL: "rol", Op.MLIL_ROR: "ror"}
_UNARY = {Op.MLIL_NEG: "neg", Op.MLIL_NOT: "not", Op.MLIL_ZX: "zx", Op.MLIL_SX: "sx", Op.MLIL_LOW_PART: "trunc"}
_COMPARE = {Op.MLIL_CMP_E: "eq", Op.MLIL_CMP_NE: "ne", Op.MLIL_CMP_ULT: "ult", Op.MLIL_CMP_ULE: "ule",
            Op.MLIL_CMP_UGT: "ugt", Op.MLIL_CMP_UGE: "uge", Op.MLIL_CMP_SLT: "slt", Op.MLIL_CMP_SLE: "sle",
            Op.MLIL_CMP_SGT: "sgt", Op.MLIL_CMP_SGE: "sge"}
_LOADS = frozenset({Op.MLIL_LOAD_SSA, Op.MLIL_LOAD_STRUCT_SSA})
SUPPORTED_OPS = frozenset(_BINARY) | frozenset(_UNARY) | frozenset(_COMPARE) | {Op.MLIL_BOOL_TO_INT}
# Division (spec v3.2 goal 1): lifted only when it cannot trap; otherwise an effect, as before.
_DIVIDE = {Op.MLIL_DIVU: "udiv", Op.MLIL_DIVS: "sdiv", Op.MLIL_MODU: "urem", Op.MLIL_MODS: "srem"}
_DIVIDE_DP = {Op.MLIL_DIVU_DP: "udiv", Op.MLIL_DIVS_DP: "sdiv", Op.MLIL_MODU_DP: "urem", Op.MLIL_MODS_DP: "srem"}
DIVISION_OPS = frozenset(_DIVIDE) | frozenset(_DIVIDE_DP)


_UNREAD = object()


@dataclass(frozen=True)
class Opaque:
    """Leaf for a pure unsupported sub-expression of the root instruction (non-SSA form)."""

    expr: MediumLevelILInstruction


def candidate_roots(instr: MediumLevelILInstruction, rejected: Optional[list] = None,
                    divisions: bool = True) -> Iterator[MediumLevelILInstruction]:
    """Maximal supported expressions that contain no effect (spec section 5.2).

    A supported expression containing a load, call or other effect is not a candidate: rewriting
    it could delete, duplicate or reorder the effect (0 & [p] -> 0). Its effect-free supported
    sub-expressions are candidates instead. Each such split is appended to `rejected` as
    (expression, reason). A division that cannot trap is pure (spec v3.2); `divisions=False` treats
    every division as an effect without proving anything (the cheap pre-check).
    """
    pure = (lambda node: is_safe_division(node, instr)) if divisions else None

    def visit(e):
        if e.operation in SUPPORTED_OPS:
            reason = mlil_effect(e.ssa_form, pure)
            if reason is None:
                yield e
                return
            if rejected is not None:
                rejected.append((e, reason))
        for operand in e.operands:
            if isinstance(operand, MediumLevelILInstruction):
                yield from visit(operand)
            elif isinstance(operand, list):
                for item in operand:
                    if isinstance(item, MediumLevelILInstruction):
                        yield from visit(item)

    yield from visit(instr)


def is_safe_division(ssa_node: MediumLevelILInstruction, root_instr: MediumLevelILInstruction) -> bool:
    """A division (SSA form) the lifter builds: one that cannot trap for any value of its inputs."""
    return ssa_node.operation in DIVISION_OPS and lift(ssa_node, root_instr=root_instr, check_versions=False) is not None


def lift_value(ssa_func, ssa_var: SSAVariable, width: int, *, root_instr: MediumLevelILInstruction,
               max_nodes: int = 200, definitions: Optional[dict] = None) -> Expr:
    """One SSA value lifted on its own, versions unchecked (a merge's incoming value, spec v3.2 goal 3):
    its definitions inlined, or the value itself as a leaf. Only a proven constant may come from it."""
    lifter = _Lifter(ssa_func, root_instr, False, max_nodes, False, definitions)
    found = lifter.var(ssa_var, width)
    if found is None:
        return Var(f"{ssa_var.var.name}#{ssa_var.version}", width, ref=ssa_var)
    return lifter.relate(found)


def lift(ssa_expr: MediumLevelILInstruction, *, root_instr: MediumLevelILInstruction,
         check_versions: bool = True, max_nodes: int = 200, effect_leaves: bool = False,
         inlined: Optional[set] = None, stats: Optional[dict] = None,
         definitions: Optional[dict] = None, keep: frozenset = frozenset(),
         shared_once: bool = False) -> Optional[Expr]:
    """`effect_leaves` is for diagnostics only: effectful sub-expressions of the root become leaves
    without a Binary Ninja ref, so the result can be classified but never lowered. `stats` receives
    the number of IL nodes visited ("visits"), failed inlining attempts included. `definitions`
    memoizes SSA definitions across lifts of one SSA form. `keep` holds (identifier, version) keys of
    SSA values left as leaves when live at the root (ILAdapter.shallow). `shared_once` (v3.1): a value
    read again costs nothing, so max_nodes bounds the distinct nodes and the result may be far larger
    as a tree (for constancy.fold_shared only). stats["budget_hit"]: the size bound left values as
    leaves (or failed the lift)."""
    lifter = _Lifter(ssa_expr.function, root_instr, check_versions, max_nodes, effect_leaves, definitions)
    lifter.keep = keep
    lifter.shared_once = shared_once
    e = lifter.expr(ssa_expr, inlined=False)
    if stats is not None:
        stats["visits"] = lifter.visits
        stats["budget_hit"] = lifter.budget_hit
        stats["stale"] = lifter.stale
    if e is not None and inlined is not None:
        inlined.update(lifter.inlined)  # (variable identifier, version) -> SSA variable, each definition followed
    return None if e is None else lifter.relate(e)


@dataclass(frozen=True)
class Backing:
    """One SSA version of a whole storage location (spec section 6.1): every bit unknown.

    `var` is a variable living in that storage, used to lower reads of it.
    """

    var: object
    version: int
    size: int  # bytes


class _Lifter:
    def __init__(self, ssa_func, root, check_versions: bool, max_nodes: int, effect_leaves: bool = False,
                 definitions: Optional[dict] = None):
        self.ssa = ssa_func
        self.root = root
        self.check = check_versions
        self.budget = max_nodes
        # Bounded work (v2.1): when values are read several times (VM handlers), the fully inlined
        # tree is exponential and a failed inlining attempt, undone, was retried at every read.
        # Failed attempts are remembered with their budget, inlined values are reused, and no new
        # attempt starts after 3 * max_nodes visits.
        self.visits = 0
        self.max_visits = 3 * max_nodes
        self.failed: dict[tuple, int] = {}  # (identifier, version, width) -> largest budget that failed
        self.done: dict[tuple, tuple] = {}  # (identifier, version, width) -> (expr, nodes, inlined)
        self.definitions = {} if definitions is None else definitions
        self.keep: frozenset = frozenset()
        self.effect_leaves = effect_leaves
        self.names: dict[tuple, str] = {}  # leaf identity -> IR name; names are labels, not identities
        self.backings: dict[tuple, Var] = {}
        self.inlined: dict[tuple[int, int], object] = {}  # (identifier, version) -> SSA variable
        self.shared_once = False
        self.budget_hit = False
        self.stale = False  # an input was overwritten before the root (version safety stopped inlining)

    def get(self, e, name: str):
        """`e.<name>`, read once per SSA form (v3.3): Binary Ninja builds a new Python object at every
        read of an operand, and roots read the same definitions again and again (snagit.dll's
        sub_180009da0 spent half its budget building them). Shared with `definitions` (other keys)."""
        key = (e.expr_index, name)
        found = self.definitions.get(key, _UNREAD)
        if found is _UNREAD:
            found = self.definitions[key] = getattr(e, name)
        return found

    def expr(self, e: MediumLevelILInstruction, *, inlined: bool) -> Optional[Expr]:
        self.visits += 1
        self.budget -= 1
        if self.budget < 0:
            self.budget_hit = True
            return None
        op, width = e.operation, e.size * 8
        if op in _COMPARE:
            return self.compare(e, inlined=inlined)
        if op == Op.MLIL_VAR_SSA and width == 0:
            return self.boolean(e, inlined=inlined)  # a flag variable (cond:0)
        if width == 0:
            return None
        if op in (Op.MLIL_CONST, Op.MLIL_CONST_PTR):
            return Const(e.constant, width)
        if op == Op.MLIL_VAR_SSA:
            return self.var(self.get(e, "src"), width)
        if op == Op.MLIL_VAR_SSA_FIELD:
            found = self.field(self.get(e, "src"), self.get(e, "offset"), width)
            if found is not None or inlined:
                return found
        elif op in _BINARY:
            left = self.expr(self.get(e, "left"), inlined=inlined)
            right = self.expr(self.get(e, "right"), inlined=inlined)
            if left is None or right is None:
                return None
            left = as_integer(left, width)  # a comparison read as an integer: 0 or 1
            if _BINARY[op] not in COUNT_OPS:
                # HLIL folds lea into x * 5 with an 8-byte constant under a 4-byte multiply.
                left, right = fit(left, width), fit(as_integer(right, width), width)
                if left is None or right is None:
                    return None
            try:
                return BinOp(_BINARY[op], left, right, width)
            except ValueError:
                return None
        elif op in _UNARY:
            operand = self.expr(self.get(e, "src"), inlined=inlined)
            return None if operand is None else unary(_UNARY[op], operand, width)
        elif op == Op.MLIL_BOOL_TO_INT:
            found = self.boolean(self.get(e, "src"), inlined=inlined)
            if found is not None:
                return UnOp("zx", found, width)
        elif op in DIVISION_OPS:
            found = self.division(e, width, inlined=inlined)
            if found is not None:
                return found
        if inlined:
            return None  # never move loads or other unsupported expressions across instructions
        if mlil_effect(e) is not None:
            # Only pure leaves may be evaluated again, or not at all.
            return Var(f"effect@{e.expr_index}", width) if self.effect_leaves else None
        return Var(f"opaque@{e.expr_index}", width, ref=Opaque(e.non_ssa_form))

    def division(self, e: MediumLevelILInstruction, width: int, *, inlined: bool) -> Optional[Expr]:
        """A division that cannot trap (spec v3.2 goal 1), else None: it stays an effect. Operands
        must have the division's own width (a division reads every bit, so a wider operand is not
        its low bits). x86's double-width forms are the narrow division when the dividend is the
        zero (unsigned) or sign (signed) extension of its low half."""
        saved, saved_inlined = self.budget, dict(self.inlined)
        double = e.operation in _DIVIDE_DP
        op = (_DIVIDE_DP if double else _DIVIDE)[e.operation]
        if double:
            dividend = self.narrow_dividend(self.get(e, "left"), op, width, inlined=inlined)
        else:
            dividend = exact(self.expr(self.get(e, "left"), inlined=inlined), width)
        divisor = exact(self.expr(self.get(e, "right"), inlined=inlined), width) if dividend is not None else None
        if divisor is not None and never_traps(op, dividend, divisor):
            return BinOp(op, dividend, divisor, width)
        self.budget, self.inlined = saved, saved_inlined
        return None

    def narrow_dividend(self, left: MediumLevelILInstruction, op: str, width: int, *, inlined: bool) -> Optional[Expr]:
        """The low half of a double-width dividend (x86 edx:eax) that is the zero (unsigned) or sign
        (signed) extension of it, else None. A dividend Binary Ninja folded to a constant is never
        used: its high half is not visible."""
        ext, wide = ("zx" if op[0] == "u" else "sx"), 2 * width
        if left.operation == Op.MLIL_VAR_SPLIT_SSA:
            whole = self.split_source(self.get(left, "high"), self.get(left, "low"), wide)
            if whole is not None:
                return narrowed(whole, width, ext)  # cdq: edx:eax = sx.q(eax)
            high = self.var(self.get(left, "high"), width)
            if ext == "zx" and high == Const(0, width):
                return self.var(self.get(left, "low"), width)  # xor edx, edx
            return None
        if left.operation in (Op.MLIL_CONST, Op.MLIL_CONST_PTR) or left.size * 8 != wide:
            return None
        whole = self.expr(left, inlined=inlined)
        return None if whole is None else narrowed(whole, width, ext)

    def split_source(self, high: SSAVariable, low: SSAVariable, wide: int) -> Optional[Expr]:
        """The value one instruction wrote to both halves of a split pair (edx:eax = sx.q(eax)),
        followed through whole copies (x86-64: temp4 = rdx; rdx = temp2), as a `wide`-bit value."""
        followed = []
        ends = []
        for ssa_var in (high, low):
            for _ in range(8):
                definition = self.definition_of(ssa_var)
                if definition is None or definition.operation != Op.MLIL_SET_VAR_SSA:
                    break
                src = self.get(definition, "src")
                if src.operation != Op.MLIL_VAR_SSA or src.size != definition.size:
                    break
                followed.append(ssa_var)
                ssa_var = self.get(src, "src")
            ends.append(ssa_var)
        definition = self.definition_of(ends[0])
        if (definition is None or definition.operation != Op.MLIL_SET_VAR_SPLIT_SSA
                or (self.get(definition, "high"), self.get(definition, "low")) != (ends[0], ends[1])
                or self.get(definition, "src").size * 8 != wide):
            return None
        whole = self.expr(self.get(definition, "src"), inlined=True)
        if whole is not None:
            for ssa_var in followed + ends:
                self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
        return whole

    def var(self, ssa_var: SSAVariable, width: int) -> Optional[Expr]:
        key = (ssa_var.var.identifier, ssa_var.version, width)
        reused = self.reuse(key)
        if reused is not None:
            return reused
        definition = self.definition_of(ssa_var)
        kept = key[:2] in self.keep and self.live(ssa_var)
        if definition is not None and not kept and self.may_inline(key):
            saved, saved_inlined = self.budget, dict(self.inlined)
            inner = self.definition(definition, ssa_var, width)
            if inner is not None:
                self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
                self.remember(key, inner, saved, saved_inlined)
                return inner
            self.failed[key] = max(self.failed.get(key, 0), saved)
            self.budget, self.inlined = saved, saved_inlined
        constant = self.dataflow_constant(ssa_var, definition, width)
        if constant is not None:
            return constant
        if not self.live(ssa_var):
            self.stale = True
            return None  # overwritten before the root and not rebuildable from live values
        # One IR name per (variable, version, width): the verifier keys samples and Z3 symbols by name.
        return Var(self.name(("var", ssa_var.var.identifier, ssa_var.version, width), f"{ssa_var.var.name}#{ssa_var.version}"),
                   width, ref=ssa_var)

    def dataflow_constant(self, ssa_var: SSAVariable, definition, width: int) -> Optional[Const]:
        """Binary Ninja's dataflow value of a leaf, when it is a single constant (v3.1): a merge of
        equal constants (phi), or a definition a rewrite made constant. Never a loaded value: memory
        may change. A constant reads no variable, so it is version-safe."""
        if (definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA
                and self.get(definition, "src").operation in _LOADS):
            return None
        try:
            value = self.ssa.get_ssa_var_value(ssa_var)
        except Exception:  # noqa: BLE001 - no dataflow for this variable
            return None
        if value.type != RegisterValueType.ConstantValue:
            return None
        return Const(value.value & ((1 << width) - 1), width)

    def definition_of(self, ssa_var: SSAVariable):
        key = (ssa_var.var.identifier, ssa_var.version)
        if key not in self.definitions:
            self.definitions[key] = self.ssa.get_ssa_var_definition(ssa_var)
        return self.definitions[key]

    def may_inline(self, key: tuple) -> bool:
        if self.budget > self.failed.get(key, 0) and self.visits < self.max_visits:
            return True
        self.budget_hit = True
        return False

    def reuse(self, key: tuple) -> Optional[Expr]:
        found = self.done.get(key)
        if found is None or (found[1] > self.budget and not self.shared_once):
            return None
        expr, nodes, inlined = found
        if not self.shared_once:
            self.budget -= nodes
        self.inlined.update(inlined)
        return expr

    def remember(self, key: tuple, expr: Expr, saved_budget: int, saved_inlined: dict) -> None:
        added = {k: v for k, v in self.inlined.items() if k not in saved_inlined}
        self.done[key] = (expr, saved_budget - self.budget, added)

    def compare(self, e: MediumLevelILInstruction, *, inlined: bool) -> Optional[Expr]:
        """A comparison: 1 bit, over operands of the comparison's own size, or of their own size
        when it has none (HLIL gives loop conditions size 0 over 4-byte operands)."""
        left, right = self.expr(self.get(e, "left"), inlined=inlined), self.expr(self.get(e, "right"), inlined=inlined)
        if left is None or right is None:
            return None
        width = e.size * 8 or max(left.width, right.width)
        # Never extends operands of two sizes, but a comparison read as an integer is 0 or 1 (as_integer).
        left, right = fit(as_integer(left, width), width), fit(as_integer(right, width), width)
        if left is None or right is None:
            return None
        return BinOp(_COMPARE[e.operation], left, right, 1)

    def boolean(self, e: MediumLevelILInstruction, *, inlined: bool) -> Optional[Expr]:
        """A 1-bit value (x86 setcc, ARM64 cset, an if's condition): a comparison, or a flag variable
        (cond:0 = t != 0), followed through its definition or kept as a 1-bit leaf. Normalization reads
        zx(t != 0) as t when t can only be 0 or 1 (v2.1's 0/1 rule)."""
        if e.operation in _COMPARE:
            return self.compare(e, inlined=inlined)
        if e.operation != Op.MLIL_VAR_SSA or e.size != 0:
            return None
        ssa_var = self.get(e, "src")
        definition = self.definition_of(ssa_var)
        if (definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA
                and _is_flag(self.get(definition, "src"))
                and self.budget > 0 and self.visits < self.max_visits
                and (ssa_var.var.identifier, ssa_var.version) not in self.keep):
            found = self.boolean(self.get(definition, "src"), inlined=True)
            if found is not None:
                self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
                return found
        if not self.live(ssa_var):
            return None
        return Var(self.name(("var", ssa_var.var.identifier, ssa_var.version, 1), f"{ssa_var.var.name}#{ssa_var.version}"),
                   1, ref=ssa_var)

    def definition(self, definition, ssa_var: SSAVariable, width: int) -> Optional[Expr]:
        if definition.operation == Op.MLIL_SET_VAR_SSA and _is_flag(self.get(definition, "src")):
            found = self.boolean(self.get(definition, "src"), inlined=True)  # r15 = cond:0, r15 = (x != 0)
            return found if found is None or width == 1 else UnOp("zx", found, width)
        if definition.operation == Op.MLIL_SET_VAR_SSA and self.get(definition, "src").size * 8 == width:
            return self.expr(self.get(definition, "src"), inlined=True)
        if definition.operation == Op.MLIL_SET_VAR_SSA_FIELD and _declared_bits(ssa_var.var) == width:
            # A write of part of the variable that keeps the rest of it: rax#2 = rax#1 with .al = e.
            value = self.written(definition)
            if value is None or 8 * self.get(definition, "offset") + value.width > width:
                return None
            old = self.var(self.get(definition, "prev"), width)
            if old is None:
                return None
            try:
                return merge_partial_write(old, value, 8 * self.get(definition, "offset"))
            except ValueError:
                return None
        return None

    def written(self, definition) -> Optional[Expr]:
        """The value a partial write (SET_VAR_SSA_FIELD) stores: rcx#2.cl = rax#2 != 0 @ rcx#1."""
        if _is_flag(self.get(definition, "src")):
            found = self.boolean(self.get(definition, "src"), inlined=True)
            return None if found is None else UnOp("zx", found, 8 * _written_bytes(definition))
        return self.expr(self.get(definition, "src"), inlined=True)

    def field(self, ssa_var: SSAVariable, offset: int, width: int) -> Optional[Expr]:
        """A read of `width` bits at byte `offset` of a variable's storage (spec section 6.1)."""
        declared = _declared_bits(ssa_var.var)
        definition = self.definition_of(ssa_var)
        kept = (ssa_var.var.identifier, ssa_var.version) in self.keep
        if (definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA_FIELD and self.budget > 0
                and self.visits < self.max_visits and not kept):
            # Read over a partial write: x86 -O0 spills bytes into one slot (var_8:7.b = y; ...).
            kind = read_over_write(offset, width // 8, self.get(definition, "offset"), _written_bytes(definition))
            saved = self.budget
            if kind == "inside":
                value = self.written(definition)
                if value is not None:
                    self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
                    return field_read(value, 8 * (offset - self.get(definition, "offset")), width)
            elif kind == "disjoint":
                found = self.field(self.get(definition, "prev"), offset, width)
                if found is not None:
                    return found
            self.budget = saved
        if declared and contained(offset, width // 8, declared // 8):
            # Within the variable itself: its definition, if any, gives every bit.
            definition = self.definition_of(ssa_var)
            if definition is not None and self.budget > 0 and self.visits < self.max_visits and not kept:
                saved = self.budget
                whole = self.definition(definition, ssa_var, declared)
                if whole is not None:
                    self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
                    return field_read(whole, 8 * offset, width)
                self.budget = saved
        backing = self.backing(self.forward(ssa_var, offset, width // 8))
        if backing is None or not contained(offset, width // 8, backing.width // 8):
            return None
        return field_read(backing, 8 * offset, width)

    def forward(self, ssa_var: SSAVariable, offset: int, size: int) -> SSAVariable:
        """The current version of the variable, if no partial write since `ssa_var` touched these
        bytes: Windows x64 -O0 spills x to var_8:6.b and then reuses rcx, so x#0 is dead at the
        root while var_8's current version still holds the byte."""
        if not self.check:
            return ssa_var
        current = self.root.get_ssa_var_version(ssa_var.var)
        version = current
        for _ in range(64):
            if version == ssa_var.version:
                return SSAVariable(ssa_var.var, current)
            definition = self.definition_of(SSAVariable(ssa_var.var, version))
            if (definition is None or definition.operation != Op.MLIL_SET_VAR_SSA_FIELD
                    or self.get(definition, "prev").var != ssa_var.var
                    or read_over_write(offset, size, self.get(definition, "offset"),
                                       _written_bytes(definition)) != "disjoint"):
                return ssa_var
            version = self.get(definition, "prev").version
        return ssa_var

    def backing(self, ssa_var: SSAVariable) -> Optional[Var]:
        var = ssa_var.var
        size = _storage_bytes(self.ssa, var)
        if size is None or not self.live(ssa_var):
            return None
        key = (int(var.source_type), var.storage, ssa_var.version)
        if key not in self.backings:
            label = f"@{_storage_name(self.ssa, var)}#{ssa_var.version}"
            self.backings[key] = Var(self.name(("backing",) + key, label), 8 * size,
                                     ref=Backing(var, ssa_var.version, size))
        return self.backings[key]

    def relate(self, e: Expr) -> Expr:
        """Express plain reads of a storage version that also has field reads through its backing
        value, so x and x.w0 are known to share their low byte."""
        groups: dict[tuple, list[Var]] = {}
        for leaf in variables(e):
            if isinstance(leaf.ref, SSAVariable):
                key = (int(leaf.ref.var.source_type), leaf.ref.var.storage, leaf.ref.version)
                groups.setdefault(key, []).append(leaf)
        mapping = {}
        for key, leaves in groups.items():
            # A version also read through fields, or read at two widths (x as 32 and 64 bits):
            # the reads share their low bits, and an IR variable has a single width.
            if key not in self.backings and len({leaf.width for leaf in leaves}) < 2:
                continue
            backing = self.backing(leaves[0].ref)
            if backing is None:
                continue
            for leaf in leaves:
                if leaf.width <= backing.width:
                    mapping[(leaf.name, leaf.width)] = field_read(backing, 0, leaf.width)
        return _substitute_leaves(e, mapping) if mapping else e

    def live(self, ssa_var: SSAVariable) -> bool:
        return not self.check or self.root.get_ssa_var_version(ssa_var.var) == ssa_var.version

    def name(self, identity: tuple, label: str) -> str:
        if identity not in self.names:
            taken = set(self.names.values())
            name = label
            while name in taken:
                name += "'"
            self.names[identity] = name
        return self.names[identity]


def _substitute_leaves(e: Expr, mapping: dict[tuple[str, int], Expr], memo: Optional[dict] = None) -> Expr:
    """Like ir.expr.substitute, keyed by (name, width): one name may be read at two widths. Each
    shared value is rebuilt once and stays shared."""
    memo = {} if memo is None else memo
    if id(e) in memo:
        return memo[id(e)]
    if isinstance(e, Var):
        out = mapping.get((e.name, e.width), e)
    elif isinstance(e, Const):
        out = e
    elif isinstance(e, UnOp):
        out = UnOp(e.op, _substitute_leaves(e.operand, mapping, memo), e.width)
    else:
        out = BinOp(e.op, _substitute_leaves(e.left, mapping, memo), _substitute_leaves(e.right, mapping, memo), e.width)
    memo[id(e)] = out
    return out


def _is_flag(e: MediumLevelILInstruction) -> bool:
    return e.operation in _COMPARE or (e.operation == Op.MLIL_VAR_SSA and e.size == 0)


def _written_bytes(definition) -> int:
    """Bytes a partial write stores: its own size (a comparison's size is its operands')."""
    return definition.size if _is_flag(definition.src) else definition.src.size


def _declared_bits(var) -> int:
    try:
        return 8 * var.type.width if var.type is not None else 0
    except Exception:
        return 0


def _storage_name(ssa_func, var) -> str:
    if var.source_type == VariableSourceType.RegisterVariableSourceType:
        return ssa_func.arch.get_reg_name(var.storage)
    return f"stack{var.storage:+#x}"


def _storage_bytes(ssa_func, var) -> Optional[int]:
    """Width of the storage a variable lives in: its register, or its own type on the stack."""
    if var.source_type == VariableSourceType.RegisterVariableSourceType:
        info = ssa_func.arch.regs.get(ssa_func.arch.get_reg_name(var.storage))
        return info.size if info is not None else None
    if var.source_type == VariableSourceType.StackVariableSourceType:
        return _declared_bits(var) // 8 or None
    return None
