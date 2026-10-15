"""ILAdapter for MLIL: the v1 pass behind the engine's IL-independent contract."""

from __future__ import annotations

from typing import Hashable, Iterator

from binaryninja import (
    ILSourceLocation, MediumLevelILFunction, MediumLevelILInstruction, MediumLevelILOperation as Op, SSAVariable,
)

from ...ir.expr import Expr, is_mba
from ..adapter import Candidate, Skip
from ..deadcode import dead_definitions
from .lift import SUPPORTED_OPS, candidate_roots, lift
from .lower import lower

_ARITH = frozenset({Op.MLIL_ADD, Op.MLIL_SUB, Op.MLIL_MUL, Op.MLIL_NEG, Op.MLIL_LSL})
_BITWISE = frozenset({Op.MLIL_AND, Op.MLIL_OR, Op.MLIL_XOR, Op.MLIL_NOT})
_PURE_LEAVES = frozenset({Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD, Op.MLIL_CONST, Op.MLIL_CONST_PTR})


class MLILAdapter:
    level = "mlil"

    def __init__(self, mlil: MediumLevelILFunction, max_nodes: int = 200):
        self.mlil = mlil
        self.max_nodes = max_nodes
        self.backups: list[tuple[int, int]] = []

    def has_candidates(self) -> bool:
        seen = set()
        for instr in self.mlil.instructions:
            rejected: list = []
            for root in candidate_roots(instr, rejected):
                seen.update(_operations(root))
            for expr, _ in rejected:
                seen.update(_operations(expr))
        return bool(seen & _ARITH) and bool(seen & _BITWISE)

    def candidates(self, skip: Skip) -> Iterator[Candidate]:
        if self.mlil.ssa_form is None:
            self.mlil.generate_ssa_form()
        for instr in list(self.mlil.instructions):
            rejected: list = []
            roots = list(candidate_roots(instr, rejected))
            for expr, reason in rejected:
                seen = lift(expr.ssa_form, root_instr=instr, max_nodes=self.max_nodes, effect_leaves=True)
                if seen is not None and is_mba(seen):
                    skip(expr.address, reason)
            for root in roots:
                inlined: set = set()
                ir = lift(root.ssa_form, root_instr=instr, max_nodes=self.max_nodes, inlined=inlined)
                if ir is not None and is_mba(ir):
                    defines, single = self._defines(instr, root)
                    yield Candidate(root, root.address, ir, defines, single, frozenset(inlined))

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


def _pure(e: MediumLevelILInstruction) -> bool:
    if e.operation in _PURE_LEAVES:
        return True
    if e.operation not in SUPPORTED_OPS:
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
