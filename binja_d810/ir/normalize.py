"""Canonical form for IR. Every rewrite is an exact identity modulo 2**width."""

from __future__ import annotations

from .evaluate import evaluate
from .expr import COMMUTATIVE_OPS, BinOp, Const, Expr, UnOp, Var, mask, to_signed, to_str

_TRUNC_THROUGH = frozenset({"add", "sub", "mul", "and", "or", "xor"})


def normalize(e: Expr) -> Expr:
    previous = None
    while e != previous:
        previous, e = e, _norm(e)
    return e


def _norm(e: Expr) -> Expr:
    if isinstance(e, (Var, Const)):
        return e
    if isinstance(e, UnOp):
        return _unary(e.op, _norm(e.operand), e.width)
    return _binary(e.op, _norm(e.left), _norm(e.right), e.width)


def _unary(op: str, a: Expr, w: int) -> Expr:
    if isinstance(a, Const):
        return Const(evaluate(UnOp(op, a, w), {}), w)
    if op in ("neg", "not") and isinstance(a, UnOp) and a.op == op:
        return a.operand
    if op == "trunc":
        return _truncate(a, w)
    if op in ("zx", "sx") and isinstance(a, UnOp) and a.op == op:
        return UnOp(op, a.operand, w)
    return UnOp(op, a, w)


def _truncate(a: Expr, w: int) -> Expr:
    if a.width == w:
        return a
    if isinstance(a, Const):
        return Const(a.value, w)
    if isinstance(a, UnOp) and a.op in ("zx", "sx"):
        inner = a.operand
        if inner.width == w:
            return inner
        if inner.width > w:
            return _truncate(inner, w)
        return UnOp(a.op, inner, w)
    if isinstance(a, UnOp) and a.op == "trunc":
        return _truncate(a.operand, w)
    if isinstance(a, UnOp) and a.op in ("neg", "not"):
        return UnOp(a.op, _truncate(a.operand, w), w)
    if isinstance(a, BinOp) and a.op in _TRUNC_THROUGH:
        return BinOp(a.op, _truncate(a.left, w), _truncate(a.right, w), w)
    return UnOp("trunc", a, w)


def _key(e: Expr) -> tuple[int, str]:
    rank = 0 if isinstance(e, Const) else 1 if isinstance(e, Var) else 2
    return rank, to_str(e)


def _is_negative(c: Const) -> bool:
    signed = to_signed(c.value, c.width)
    return signed < 0 and c.value != 1 << (c.width - 1)


def _binary(op: str, a: Expr, b: Expr, w: int) -> Expr:
    m = mask(w)
    if isinstance(a, Const) and isinstance(b, Const):
        return Const(evaluate(BinOp(op, a, b, w), {}), w)
    if op == "shl" and isinstance(b, Const) and b.value < w:
        return _binary("mul", Const(1 << b.value, w), a, w)
    if op == "sub":
        if a == b:
            return Const(0, w)
        if isinstance(a, Const) and a.value == 0:
            return UnOp("neg", b, w)
        if isinstance(b, Const) and b.value == 0:
            return a
        if isinstance(b, Const) and _is_negative(b):
            return _binary("add", a, Const(-b.value, w), w)
        if isinstance(b, UnOp) and b.op == "neg":
            return _binary("add", a, b.operand, w)
    if op in COMMUTATIVE_OPS and _key(b) < _key(a):
        a, b = b, a
    if isinstance(a, Const) and isinstance(b, BinOp) and b.op == op and op in COMMUTATIVE_OPS \
            and isinstance(b.left, Const):
        # c2 op (c1 op x) == (c1 op c2) op x: x86's shift-count mask on an already masked count.
        return _binary(op, Const(evaluate(BinOp(op, a, b.left, w), {}), w), b.right, w)
    if isinstance(a, Const):
        c = a.value
        if op in ("add", "or", "xor") and c == 0:
            return b
        if (op == "and" and c == m) or (op == "mul" and c == 1):
            return b
        if op in ("and", "mul") and c == 0:
            return Const(0, w)
        if op == "or" and c == m:
            return Const(m, w)
        if op == "xor" and c == m:
            return UnOp("not", b, w)
        if op == "mul" and c == m:
            return UnOp("neg", b, w)
        if op == "add" and _is_negative(a):
            return BinOp("sub", b, Const(-c, w), w)
    if op == "add":
        if isinstance(b, UnOp) and b.op == "neg":
            return BinOp("sub", a, b.operand, w)
        if isinstance(a, UnOp) and a.op == "neg":
            return BinOp("sub", b, a.operand, w)
    if op == "xor" and a == b:
        return Const(0, w)
    if op in ("and", "or") and a == b:
        return a
    return BinOp(op, a, b, w)
