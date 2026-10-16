"""Concrete evaluation of IR modulo 2**width (shift counts: see shift_count)."""

from __future__ import annotations

from typing import Mapping

from .expr import CMP_OPS, BinOp, Const, Expr, UnOp, Var, mask, to_signed


def evaluate(e: Expr, env: Mapping[str, int]) -> int:
    if isinstance(e, Var):
        return env[e.name] & mask(e.width)
    if isinstance(e, Const):
        return e.value
    m = mask(e.width)
    if isinstance(e, UnOp):
        v = evaluate(e.operand, env)
        if e.op == "neg":
            return -v & m
        if e.op == "not":
            return ~v & m
        if e.op == "sx":
            return to_signed(v, e.operand.width) & m
        return v & m  # zx, trunc
    assert isinstance(e, BinOp)
    x, y = evaluate(e.left, env), evaluate(e.right, env)
    if e.op == "add":
        return (x + y) & m
    if e.op == "sub":
        return (x - y) & m
    if e.op == "mul":
        return (x * y) & m
    if e.op == "and":
        return x & y
    if e.op == "or":
        return x | y
    if e.op == "xor":
        return x ^ y
    if e.op in ("rol", "ror"):
        return _rotate(e.op, x, y, e.width)
    if e.op in CMP_OPS:
        return _compare(e.op, x, y, e.left.width)
    return _shift(e.op, x, y, e.width)


def _compare(op: str, x: int, y: int, width: int) -> int:
    if op[0] == "s":
        x, y = to_signed(x, width), to_signed(y, width)
    return int({"eq": x == y, "ne": x != y, "ult": x < y, "ule": x <= y, "ugt": x > y, "uge": x >= y,
                "slt": x < y, "sle": x <= y, "sgt": x > y, "sge": x >= y}[op])


def shift_count(count: int, width: int) -> int:
    """Binary Ninja's IL takes a shift count modulo max(32, width), never modulo an 8- or 16-bit
    width: x86 `shl al, cl` by 8 clears al, ARM64 `lsl w0, w0, w1` by 33 shifts by 1 (both measured
    with Binary Ninja's dataflow, see tests/unit/test_shifts.py). x86's own count masks are explicit
    `and` nodes in the IL."""
    return count % max(32, width)


def _rotate(op: str, x: int, count: int, width: int) -> int:
    """Rotates take the count modulo the width (Binary Ninja's dataflow, x86 and ARM64 alike)."""
    c, m = count % width, mask(width)
    if c == 0:
        return x & m
    if op == "rol":
        return ((x << c) | (x >> (width - c))) & m
    return ((x >> c) | (x << (width - c))) & m


def _shift(op: str, x: int, count: int, width: int) -> int:
    c = shift_count(count, width)
    m = mask(width)
    if op == "shl":
        return (x << c) & m
    if op == "lshr":
        return x >> c
    return (to_signed(x, width) >> c) & m  # ashr
