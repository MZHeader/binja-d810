"""Canonical form for IR. Every rewrite is an exact identity modulo 2**width."""

from __future__ import annotations

from .bits import compare_with_zero, decided_comparison, possible_ones
from .evaluate import evaluate
from .expr import CMP_OPS, COMMUTATIVE_OPS, DIV_OPS, BinOp, Const, Expr, UnOp, Var, mask, to_signed, to_str

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
    if op == "zx" and isinstance(a, BinOp) and a.op in ("eq", "ne"):
        # bool_to_int of a 0/1 value tested against zero is the value or its complement
        for value, other in ((a.left, a.right), (a.right, a.left)):
            if isinstance(other, Const) and other.value == 0:
                found = compare_with_zero(value, a.op, w)
                if found is not None:
                    return _norm(found)
    if op == "zx":
        widened = _widen(a, w)
        if widened is not None and possible_ones(widened) >> a.width == 0:
            return widened
    return UnOp(op, a, w)


def _widen(e: Expr, w: int):
    """An expression of width w whose low e.width bits are e: truncations of values at least w bits
    wide are undone, through constants and bitwise operations (which commute with truncation).
    If it can have no one above e.width, it is zx(e) itself: x86 setne r15b; movzx r15d, r15b over
    x & 1 gives zx32(1 & trunc8(x & 1)), which is x & 1."""
    if isinstance(e, Const):
        return Const(e.value, w)
    if isinstance(e, UnOp) and e.op == "trunc" and e.operand.width >= w:
        return _truncate(e.operand, w)
    if isinstance(e, BinOp) and e.op in ("and", "or", "xor"):
        left, right = _widen(e.left, w), _widen(e.right, w)
        return None if left is None or right is None else BinOp(e.op, left, right, w)
    return None


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
    if (isinstance(a, BinOp) and a.op == "lshr" and isinstance(a.right, Const) and isinstance(a.left, UnOp)
            and a.left.op == "trunc" and a.right.value + w <= a.width):
        # trunc_w(trunc_v(x) >> c) with the window inside v: the same bits of x (rdx = x.di; rdx.dh).
        inner = a.left.operand
        return _truncate(BinOp("lshr", inner, Const(a.right.value, inner.width), inner.width), w)
    if isinstance(a, BinOp) and a.op in DIV_OPS:
        # A division of extended values is the narrow division, at every value:
        # x86's divu.dp.d(0:x, y) and divs.dp.d(sx.q(x), y).
        ext = "zx" if a.op[0] == "u" else "sx"
        left, right = narrowed(a.left, w, ext), narrowed(a.right, w, ext)
        if left is not None and right is not None:
            return BinOp(a.op, left, right, w)
    return UnOp("trunc", a, w)


def narrowed(e: Expr, w: int, ext: str):
    """The w-bit value `e` is the `ext` extension of, or None."""
    if isinstance(e, UnOp) and e.op == ext and e.operand.width == w:
        return e.operand
    if isinstance(e, Const):
        narrow = Const(e.value, w)
        if evaluate(UnOp(ext, narrow, e.width), {}) == e.value:
            return narrow
    return None


def _compare(op: str, a: Expr, b: Expr) -> Expr:
    """Comparisons decided by their form: equal operands, and unsigned bounds from known bits."""
    decided = decided_comparison(op, a, b)
    return Const(decided, 1) if decided is not None else BinOp(op, a, b, 1)


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
    if op in CMP_OPS:
        return _compare(op, a, b)
    if op == "shl" and isinstance(b, Const) and b.value < w:
        return _binary("mul", Const(1 << b.value, w), a, w)
    if op == "sub":
        if a == b:
            return Const(0, w)
        if isinstance(b, Const) and isinstance(a, BinOp) and a.op == "sub" and isinstance(a.right, Const):
            return _binary("sub", a.left, Const(a.right.value + b.value, w), w)  # (x - c1) - c2
        if isinstance(b, Const) and isinstance(a, BinOp) and a.op == "add" and isinstance(a.left, Const):
            return _binary("add", Const(a.left.value - b.value, w), a.right, w)  # (c1 + x) - c2
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
    if op == "add" and isinstance(a, Const) and isinstance(b, BinOp) and b.op == "sub" and isinstance(b.right, Const):
        return _binary("sub", b.left, Const(b.right.value - a.value, w), w)  # c2 + (x - c1)
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
