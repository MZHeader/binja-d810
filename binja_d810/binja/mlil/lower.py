"""binja_d810 IR -> new MLIL expression in the function being analyzed."""

from __future__ import annotations

from binaryninja import ExpressionIndex, ILSourceLocation, MediumLevelILFunction, SSAVariable

from ...ir.expr import BinOp, Const, Expr, UnOp, Var
from .lift import Backing, Opaque
from ..storage import as_field


def lower(mlil: MediumLevelILFunction, e: Expr, loc: ILSourceLocation) -> ExpressionIndex:
    size = e.width // 8
    field = as_field(e)
    if field is not None and isinstance(field[0].ref, Backing):
        backing, offset, width = field
        var = backing.ref.var
        if offset == 0 and width == 8 * var.type.width:
            return mlil.var(size, var, loc)
        return mlil.var_field(size, var, offset // 8, loc)
    if isinstance(e, Const):
        return mlil.const(size, e.value, loc)
    if isinstance(e, Var):
        if isinstance(e.ref, SSAVariable):
            return mlil.var(size, e.ref.var, loc)
        if isinstance(e.ref, Opaque):
            return e.ref.expr.copy_to(mlil)
        raise ValueError(f"cannot lower variable without a Binary Ninja ref: {e.name}")
    if isinstance(e, UnOp):
        operand = lower(mlil, e.operand, loc)
        build = {"neg": mlil.neg_expr, "not": mlil.not_expr, "zx": mlil.zero_extend,
                 "sx": mlil.sign_extend, "trunc": mlil.low_part}[e.op]
        return build(size, operand, loc)
    assert isinstance(e, BinOp)
    left, right = lower(mlil, e.left, loc), lower(mlil, e.right, loc)
    build = {"add": mlil.add, "sub": mlil.sub, "mul": mlil.mult, "and": mlil.and_expr, "or": mlil.or_expr,
             "xor": mlil.xor_expr, "shl": mlil.shift_left, "lshr": mlil.logical_shift_right,
             "ashr": mlil.arith_shift_right}[e.op]
    return build(size, left, right, loc)
