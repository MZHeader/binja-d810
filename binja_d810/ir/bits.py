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


_MIRRORED = {"eq": "eq", "ne": "ne", "ult": "ugt", "ule": "uge", "ugt": "ult", "uge": "ule"}


def decided_comparison(op: str, a: Expr, b: Expr) -> Optional[int]:
    """The value of `op(a, b)` when its form decides it: equal operands, or an unsigned comparison
    (or equality) with a constant that the other side's possible bits put out of reach, as in
    zx.d(x.b) u> 0xff. None otherwise; signed comparisons are decided only by equal operands."""
    if a == b:
        return int(op in ("eq", "ule", "uge", "sle", "sge"))
    if isinstance(a, Const) and not isinstance(b, Const) and op in _MIRRORED:
        op, a, b = _MIRRORED[op], b, a
    if not isinstance(b, Const) or op not in _MIRRORED:
        return None
    top, c = possible_ones(a), b.value  # top bounds a: a <= top (unsigned)
    if op in ("eq", "ne"):
        return int(op == "ne") if c & ~top else None
    if op == "ult":
        return 1 if top < c else 0 if c == 0 else None
    if op == "uge":
        return 0 if top < c else 1 if c == 0 else None
    if op == "ule":
        return 1 if top <= c else None
    return 0 if top <= c else None  # ugt


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
