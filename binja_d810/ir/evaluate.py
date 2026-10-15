"""Concrete evaluation of IR modulo 2**width (shift counts: see shift_count)."""

from __future__ import annotations

from typing import Mapping

from .expr import BinOp, Const, Expr, UnOp, Var, mask, to_signed


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
    return _shift(e.op, x, y, e.width)


def shift_count(count: int, width: int) -> int:
    """Binary Ninja's IL takes a shift count modulo max(32, width), never modulo an 8- or 16-bit
    width: x86 `shl al, cl` by 8 clears al, ARM64 `lsl w0, w0, w1` by 33 shifts by 1 (both measured
    with Binary Ninja's dataflow, see tests/unit/test_shifts.py). x86's own count masks are explicit
    `and` nodes in the IL."""
    return count % max(32, width)


def _shift(op: str, x: int, count: int, width: int) -> int:
    c = shift_count(count, width)
    m = mask(width)
    if op == "shl":
        return (x << c) & m
    if op == "lshr":
        return x >> c
    return (to_signed(x, width) >> c) & m  # ashr
