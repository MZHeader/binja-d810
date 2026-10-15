"""Backing-storage model for field reads and partial writes (spec section 6.1). No binaryninja import.

Each SSA version of a storage location (a register, or a stack variable) has one backing value of
the storage's full width, whose bits are all unknown. A read of `size` bytes at byte `offset` is
`trunc(backing >> 8*offset)`; a variable read at its declared width is the read at offset 0. Reads
of the same storage version are therefore related exactly (x == trunc8(x.w0)), and no read ever
assumes anything about the bits it does not cover: zero or sign extension appears only when the IL
says so.
"""

from __future__ import annotations

from typing import Optional

from ..ir.expr import BinOp, Const, Expr, UnOp, Var, mask


def contained(offset: int, size: int, storage_size: int) -> bool:
    """A read of `size` bytes at byte `offset` lies within `storage_size` bytes."""
    return size > 0 and offset >= 0 and offset + size <= storage_size


def field_read(backing: Expr, offset_bits: int, width: int) -> Expr:
    if not (0 <= offset_bits and offset_bits + width <= backing.width):
        raise ValueError(f"{width}-bit read at bit {offset_bits} exceeds {backing.width} bits")
    e = backing
    if offset_bits:
        e = BinOp("lshr", e, Const(offset_bits, backing.width), backing.width)
    if width < backing.width:
        e = UnOp("trunc", e, width)
    return e


def merge_partial_write(old: Expr, value: Expr, offset_bits: int) -> Expr:
    """The storage after writing `value` at bit `offset_bits`, keeping the other bits of `old`.

    For an IL write that preserves the rest of the storage (SET_VAR_FIELD). A zero-extending write
    is an ordinary assignment of a zx in the IL and never comes here.
    """
    big, small = old.width, value.width
    if offset_bits < 0 or offset_bits + small > big:
        raise ValueError(f"{small}-bit write at bit {offset_bits} exceeds {big} bits")
    if small == big:
        return value
    field_mask = mask(small) << offset_bits
    widened = UnOp("zx", value, big)
    if offset_bits:
        widened = BinOp("shl", widened, Const(offset_bits, big), big)
    kept = BinOp("and", old, Const(~field_mask & mask(big), big), big)
    return BinOp("or", kept, BinOp("and", widened, Const(field_mask, big), big), big)


def as_field(e: Expr) -> Optional[tuple[Var, int, int]]:
    """(variable, offset in bits, width) if `e` is a read of part of one variable, else None."""
    if isinstance(e, Var):
        return e, 0, e.width
    if isinstance(e, UnOp) and e.op == "trunc":
        inner, offset = e.operand, 0
        if isinstance(inner, BinOp) and inner.op == "lshr" and isinstance(inner.right, Const):
            inner, offset = inner.left, inner.right.value
        if isinstance(inner, Var) and offset % 8 == 0 and offset + e.width <= inner.width:
            return inner, offset, e.width
    return None
