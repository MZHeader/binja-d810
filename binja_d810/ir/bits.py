"""Known bits: which bits of an expression can be one (a sound over-approximation)."""

from __future__ import annotations

from typing import Optional

from .evaluate import shift_count
from .expr import CMP_OPS, BinOp, Const, Expr, UnOp, Var, mask


def possible_ones(e: Expr) -> int:
    """A mask with every bit that can be one in some evaluation of `e`."""
    m = mask(e.width)
    if isinstance(e, Const):
        return e.value
    if isinstance(e, Var):
        return m
    if isinstance(e, UnOp):
        inner = possible_ones(e.operand)
        if e.op in ("zx", "trunc"):
            return inner & m
        if e.op == "sx":
            sign = 1 << (e.operand.width - 1)
            return inner | (m & ~mask(e.operand.width)) if inner & sign else inner
        return m  # neg, not
    assert isinstance(e, BinOp)
    if e.op in CMP_OPS:
        return 1
    left, right = possible_ones(e.left), possible_ones(e.right)
    if e.op == "and":
        return left & right
    if e.op in ("or", "xor"):
        return left | right
    if e.op in ("shl", "lshr") and isinstance(e.right, Const):
        count = shift_count(e.right.value, e.width)
        return (left << count) & m if e.op == "shl" else left >> count
    return m


def compare_with_zero(value: Expr, kind: str, width: int) -> Optional[Expr]:
    """`value != 0` (kind "ne") or `value == 0` ("eq") as a `width`-bit 0 or 1 (x86 setcc, ARM64
    cset), when `value` itself can only be 0 or 1: then it is `value` or `value ^ 1`. Other
    comparisons are not modelled (spec v3)."""
    if possible_ones(value) & ~1:
        return None
    if value.width > width:
        value = UnOp("trunc", value, width)
    elif value.width < width:
        value = UnOp("zx", value, width)
    return value if kind == "ne" else BinOp("xor", value, Const(1, width), width)
