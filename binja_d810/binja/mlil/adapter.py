"""ILAdapter for MLIL: the v1 pass behind the engine's IL-independent contract."""

from __future__ import annotations

from typing import Hashable, Iterator, Optional

from binaryninja import (
    ILSourceLocation, MediumLevelILFunction, MediumLevelILInstruction, MediumLevelILOperation as Op, SSAVariable,
)

from ...deadline import Deadline
from ...ir.expr import Expr, is_mba
from ..adapter import Candidate, Skip
from ..deadcode import dead_definitions, dying_with_root
from .lift import SUPPORTED_OPS, candidate_roots, lift
from .lower import lower

_ARITH = frozenset({Op.MLIL_ADD, Op.MLIL_SUB, Op.MLIL_MUL, Op.MLIL_NEG, Op.MLIL_LSL})
_BITWISE = frozenset({Op.MLIL_AND, Op.MLIL_OR, Op.MLIL_XOR, Op.MLIL_NOT})
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
            for root in candidate_roots(instr, rejected):
                seen.update(_operations(root))
            for expr, _ in rejected:
                seen.update(_operations(expr))
        return bool(seen & _ARITH) and bool(seen & _BITWISE)

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
                if seen is not None and is_mba(seen):
                    skip(expr.address, reason)
            for root in reversed(roots):
                inlined: dict = {}
                ir = lift(root.ssa_form, root_instr=instr, max_nodes=self.max_nodes, inlined=inlined,
                          definitions=definitions)
                if ir is not None and is_mba(ir):
                    defines, single = self._defines(instr, root)
                    nodes, mixed, dying = self._visible(instr, root, inlined)
                    yield Candidate(root, root.address, ir, defines, single, frozenset(inlined), nodes, mixed,
                                    instr, frozenset(inlined) - dying)

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
        return Candidate(root, root.address, ir, defines, single, frozenset(inlined), nodes, mixed, instr)

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
        # Mixed if one statement is: an arithmetic definition and a bitwise use are each unmixed, and
        # merging them into one mixed expression is what the gate refuses (snagit.dll).
        mixed = any(bool(set(_operations(tree)) & _ARITH) and bool(set(_operations(tree)) & _BITWISE) for tree in trees)
        return sum(_size(tree) for tree in trees), mixed, dying

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
            else:
                roots.extend(_key(v) for v in _ssa_vars(instr))
        return {index_of[key] for key in dead_definitions(pure, roots)}

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
