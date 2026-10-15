"""binja_d810 IR -> new HLIL expressions, built with HighLevelILFunction's builders."""

from __future__ import annotations

from binaryninja import ExpressionIndex, HighLevelILFunction, HighLevelILOperation as H, ILSourceLocation

from ...ir.expr import BinOp, Const, Expr, UnOp, Var
from ..storage import as_field
from .lift import FieldRef, HLILBacking

_UNARY = {"neg": "neg_expr", "not": "not_expr", "zx": "zero_extend", "sx": "sign_extend", "trunc": "low_part"}
_BINARY = {"add": "add", "sub": "sub", "mul": "mult", "and": "and_expr", "or": "or_expr", "xor": "xor_expr",
           "shl": "shift_left", "lshr": "logical_shift_right", "ashr": "arith_shift_right"}


def lower(hlil: HighLevelILFunction, e: Expr, loc: ILSourceLocation) -> ExpressionIndex:
    size = e.width // 8
    field = as_field(e)
    if field is not None and isinstance(field[0].ref, HLILBacking):
        backing, offset, width = field
        if offset:  # related reads are always at offset 0 (lift._relate)
            raise ValueError(f"cannot lower a read at bit {offset} of {backing.name}")
        return hlil.var(width // 8, backing.ref.var, loc)  # the variable read at that size, as in the IL
    if isinstance(e, Const):
        return hlil.const(size, e.value, loc)
    if isinstance(e, Var):
        if isinstance(e.ref, FieldRef):
            ref = e.ref
            var = hlil.var(ref.var.type.width if ref.var.type is not None else size, ref.var, loc)
            return hlil.expr(H.HLIL_STRUCT_FIELD, var, ref.offset, ref.member, size=ref.size, source_location=loc)
        var = getattr(e.ref, "var", None)
        if var is None:
            raise ValueError(f"cannot lower variable without a Binary Ninja ref: {e.name}")
        return hlil.var(size, var, loc)
    if isinstance(e, UnOp):
        return getattr(hlil, _UNARY[e.op])(size, lower(hlil, e.operand, loc), loc)
    assert isinstance(e, BinOp)
    return getattr(hlil, _BINARY[e.op])(size, lower(hlil, e.left, loc), lower(hlil, e.right, loc), loc)
