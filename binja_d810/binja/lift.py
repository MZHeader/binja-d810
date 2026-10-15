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

from binaryninja import MediumLevelILInstruction, MediumLevelILOperation as Op, SSAVariable

from ..ir.expr import BinOp, Const, Expr, UnOp, Var

_BINARY = {Op.MLIL_ADD: "add", Op.MLIL_SUB: "sub", Op.MLIL_MUL: "mul", Op.MLIL_AND: "and",
           Op.MLIL_OR: "or", Op.MLIL_XOR: "xor", Op.MLIL_LSL: "shl", Op.MLIL_LSR: "lshr", Op.MLIL_ASR: "ashr"}
_UNARY = {Op.MLIL_NEG: "neg", Op.MLIL_NOT: "not", Op.MLIL_ZX: "zx", Op.MLIL_SX: "sx", Op.MLIL_LOW_PART: "trunc"}
SUPPORTED_OPS = frozenset(_BINARY) | frozenset(_UNARY)


@dataclass(frozen=True)
class Opaque:
    """Leaf for an unsupported sub-expression of the root instruction (non-SSA form)."""

    expr: MediumLevelILInstruction


def candidate_roots(instr: MediumLevelILInstruction) -> Iterator[MediumLevelILInstruction]:
    def visit(e):
        if e.operation in SUPPORTED_OPS:
            yield e
            return
        for operand in e.operands:
            if isinstance(operand, MediumLevelILInstruction):
                yield from visit(operand)
            elif isinstance(operand, list):
                for item in operand:
                    if isinstance(item, MediumLevelILInstruction):
                        yield from visit(item)

    yield from visit(instr)


def lift(ssa_expr: MediumLevelILInstruction, *, root_instr: MediumLevelILInstruction,
         check_versions: bool = True, max_nodes: int = 200) -> Optional[Expr]:
    return _Lifter(ssa_expr.function, root_instr, check_versions, max_nodes).expr(ssa_expr, inlined=False)


class _Lifter:
    def __init__(self, ssa_func, root, check_versions: bool, max_nodes: int):
        self.ssa = ssa_func
        self.root = root
        self.check = check_versions
        self.budget = max_nodes

    def expr(self, e: MediumLevelILInstruction, *, inlined: bool) -> Optional[Expr]:
        self.budget -= 1
        if self.budget < 0:
            return None
        op, width = e.operation, e.size * 8
        if width == 0:
            return None
        if op in (Op.MLIL_CONST, Op.MLIL_CONST_PTR):
            return Const(e.constant, width)
        if op == Op.MLIL_VAR_SSA:
            return self.var(e.src, width)
        if op in _BINARY:
            left = self.expr(e.left, inlined=inlined)
            right = self.expr(e.right, inlined=inlined)
            if left is None or right is None:
                return None
            try:
                return BinOp(_BINARY[op], left, right, width)
            except ValueError:
                return None
        if op in _UNARY:
            operand = self.expr(e.src, inlined=inlined)
            if operand is None:
                return None
            try:
                return UnOp(_UNARY[op], operand, width)
            except ValueError:
                return None
        if inlined:
            return None  # never move loads or other unsupported expressions across instructions
        return Var(f"opaque@{e.expr_index}", width, ref=Opaque(e.non_ssa_form))

    def var(self, ssa_var: SSAVariable, width: int) -> Optional[Expr]:
        definition = self.ssa.get_ssa_var_definition(ssa_var)
        if (definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA
                and definition.src.size * 8 == width and self.budget > 0):
            saved = self.budget
            inner = self.expr(definition.src, inlined=True)
            if inner is not None:
                return inner
            self.budget = saved
        if self.check and self.root.get_ssa_var_version(ssa_var.var) != ssa_var.version:
            return None  # overwritten before the root and not rebuildable from live values
        return Var(f"{ssa_var.var.name}#{ssa_var.version}", width, ref=ssa_var)
