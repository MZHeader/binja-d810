"""Concrete evaluation of IR modulo 2**width."""

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
    shift = y % e.width
    if e.op == "shl":
        return (x << shift) & m
    if e.op == "lshr":
        return x >> shift
    return (to_signed(x, e.width) >> shift) & m  # ashr
