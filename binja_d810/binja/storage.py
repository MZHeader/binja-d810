"""Backing-storage model for field reads and partial writes. No binaryninja import.

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


def read_over_write(read_offset: int, read_size: int, write_offset: int, write_size: int) -> str:
    """How a read of bytes relates to the last partial write before it: "inside" (the written value
    alone determines it), "disjoint" (the previous version alone does) or "partial" (both)."""
    if write_offset <= read_offset and read_offset + read_size <= write_offset + write_size:
        return "inside"
    if read_offset + read_size <= write_offset or read_offset >= write_offset + write_size:
        return "disjoint"
    return "partial"


def fit(e: Expr, width: int) -> Optional[Expr]:
    """An operand of a `width`-bit +, -, *, &, | or ^ whose IL size differs from the operation's.

    Wider: its low bits, exactly (those operations never look at higher bits). Narrower: None,
    since the extension is not stated by the IL and is never guessed."""
    if e.width == width:
        return e
    if e.width < width:
        return None
    if isinstance(e, Const):
        return Const(e.value, width)
    return UnOp("trunc", e, width)


def as_integer(e: Expr, width: int) -> Expr:
    """A 1-bit value (a comparison or a flag) read as a `width`-bit integer. Binary Ninja gives a
    comparison the value 0 or 1, whatever reads it, so this is its zero extension: (y s< 10).b
    (MSVC's setl r8b), sx.q(x < y) (1, never -1)."""
    return UnOp("zx", e, width) if e.width == 1 and width > 1 else e


def unary(op: str, operand: Expr, width: int) -> Optional[Expr]:
    """An IL unary operation of `width` bits on a lifted operand; None if the widths do not fit."""
    if operand.width == 1 and width > 1:
        operand = as_integer(operand, width)
        if op in ("zx", "sx", "trunc"):
            return operand
    try:
        return UnOp(op, operand, width)
    except ValueError:
        return None


def exact(e: Optional[Expr], width: int) -> Optional[Expr]:
    """`e` as a division operand of `width` bits: its own width, or a wider constant whose
    value fits. Never `fit`: a division reads every bit, so a wider operand's low bits are not its value."""
    if e is None or e.width == width:
        return e
    if isinstance(e, Const) and e.width > width and e.value >> width == 0:
        return Const(e.value, width)
    return None  # a narrower constant is never widened: its extension is not stated


def visible_size(e: Expr) -> int:
    """Nodes as they will appear in the IL: a read of part of a variable (trunc(b >> 8), lowered as
    one field read) counts once."""
    if as_field(e) is not None:
        return 1
    if isinstance(e, BinOp):
        return 1 + visible_size(e.left) + visible_size(e.right)
    if isinstance(e, UnOp):
        return 1 + visible_size(e.operand)
    return 1
