"""Concrete evaluation of IR modulo 2**width (shift counts: see shift_count)."""

from __future__ import annotations

from typing import Mapping

from .expr import CMP_OPS, DIV_OPS, BinOp, Const, Expr, UnOp, Var, children, mask, to_signed


def evaluate(e: Expr, env: Mapping[str, int]) -> int:
    if isinstance(e, Var):
        return env[e.name] & mask(e.width)
    if isinstance(e, Const):
        return e.value
    if isinstance(e, UnOp):
        return _unary(e, evaluate(e.operand, env))
    assert isinstance(e, BinOp)
    return _binary(e, evaluate(e.left, env), evaluate(e.right, env))


def evaluate_shared(e: Expr, env: Mapping[str, int]) -> int:
    """evaluate, each shared value computed once (iterative): linear in the distinct nodes."""
    memo: dict[int, int] = {}
    stack = [(e, False)]
    while stack:
        node, ready = stack.pop()
        if id(node) in memo:
            continue
        if isinstance(node, Var):
            memo[id(node)] = env[node.name] & mask(node.width)
        elif isinstance(node, Const):
            memo[id(node)] = node.value
        elif not ready:
            stack.append((node, True))
            stack.extend((k, False) for k in children(node) if id(k) not in memo)
        elif isinstance(node, UnOp):
            memo[id(node)] = _unary(node, memo[id(node.operand)])
        else:
            memo[id(node)] = _binary(node, memo[id(node.left)], memo[id(node.right)])
    return memo[id(e)]


def _unary(e: UnOp, v: int) -> int:
    m = mask(e.width)
    if e.op == "neg":
        return -v & m
    if e.op == "not":
        return ~v & m
    if e.op == "sx":
        return to_signed(v, e.operand.width) & m
    return v & m  # zx, trunc


def _binary(e: BinOp, x: int, y: int) -> int:
    m = mask(e.width)
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
    if e.op in DIV_OPS:
        return _divide(e.op, x, y, e.width)
    return _shift(e.op, x, y, e.width)


def _divide(op: str, x: int, y: int, width: int) -> int:
    """ARM64's division, total (spec v3.2): x / 0 = 0, x % 0 = x, MIN / -1 = MIN, MIN % -1 = 0. Signed
    division rounds toward zero; the remainder takes the dividend's sign (x86 and C as well)."""
    if y == 0:
        return 0 if op in ("udiv", "sdiv") else x
    if op == "udiv":
        return x // y
    if op == "urem":
        return x % y
    sx, sy = to_signed(x, width), to_signed(y, width)
    q = abs(sx) // abs(sy)
    if (sx < 0) != (sy < 0):
        q = -q
    return (q if op == "sdiv" else sx - q * sy) & mask(width)


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
