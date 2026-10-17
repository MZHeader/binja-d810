"""Match d810 DSL patterns (vendored SymbolicExpression trees) against normalized IR."""

from __future__ import annotations

from typing import Any, Iterator, Mapping, Optional

from ..ir.expr import CMP_OPS, BinOp, Const, Expr, UnOp, mask
from ..ir.normalize import normalize

D810_BINARY = {"add": "add", "sub": "sub", "mul": "mul", "and": "and", "or": "or", "xor": "xor",
               "shl": "shl", "shr": "lshr", "sar": "ashr"}
D810_UNARY = {"neg": "neg", "bnot": "not"}
_COMMUTATIVE = frozenset({"add", "mul", "and", "or", "xor"})

Bindings = dict[str, Expr]


def match(pattern: Any, target: Expr, bindings: Optional[Mapping[str, Expr]] = None) -> Iterator[Bindings]:
    yield from _match(pattern, target, dict(bindings or {}))


def _negate(e: Expr) -> Expr:
    return normalize(UnOp("neg", e, e.width))


def _bind(name: str, value: Expr, b: Bindings) -> Optional[Bindings]:
    if name in b:
        return b if b[name] == value else None
    out = dict(b)
    out[name] = value
    return out


def _concrete(p: Any) -> Optional[int]:
    """Value of a concrete-constant leaf (e.g. TWO), else None."""
    if p.operation is None and p.value is not None:
        return p.value
    return None


def _pairs(pairs: list, b: Bindings) -> Iterator[Bindings]:
    if not pairs:
        yield b
        return
    (pattern, target), rest = pairs[0], pairs[1:]
    for b2 in _match(pattern, target, b):
        yield from _pairs(rest, b2)


def _match_leaf(p: Any, t: Expr, b: Bindings) -> Iterator[Bindings]:
    if p.value is not None:
        if isinstance(t, Const) and t.value == p.value & mask(t.width):
            yield b
        return
    if getattr(p, "is_pattern_constant", False):
        if isinstance(t, Const):
            out = _bind(p.name, t, b)
            if out is not None:
                yield out
        return
    out = _bind(p.name, t, b)
    if out is not None:
        yield out


# d810's constraint comparisons are unsigned (its Z3 visitor uses ULT); == is an EqualityConstraint.
_COMPARISONS = {"ne": "ne", "lt": "ult", "le": "ule", "gt": "ugt", "ge": "uge"}


def comparison_of(constraint: Any) -> Optional[str]:
    """The IR comparison a d810 bool_to_int constraint tests, or None."""
    kind = type(constraint).__name__
    if kind == "EqualityConstraint":
        return "eq"
    if kind == "ComparisonConstraint":
        return _COMPARISONS.get(constraint.op_name)
    return None


def _match_bool(p: Any, t: Expr, b: Bindings) -> Iterator[Bindings]:
    """(l op r).to_int() matches zx(cmp) at the pattern's width, and a bare 1-bit cmp (a branch
    condition or flag)."""
    inner = t.operand if isinstance(t, UnOp) and t.op == "zx" else t
    if not (isinstance(inner, BinOp) and inner.op in CMP_OPS) or inner.op != comparison_of(p.constraint):
        return
    sides = (p.constraint.left, p.constraint.right)
    yield from _pairs([(sides[0], inner.left), (sides[1], inner.right)], b)
    if inner.op in ("eq", "ne"):
        yield from _pairs([(sides[0], inner.right), (sides[1], inner.left)], b)


def _match(p: Any, t: Expr, b: Bindings) -> Iterator[Bindings]:
    op = p.operation
    if op is None:
        yield from _match_leaf(p, t, b)
        return
    if op == "bool_to_int":
        yield from _match_bool(p, t, b)
        return
    if op == "zext":
        if isinstance(t, UnOp) and t.op == "zx" and t.width == p.value:
            yield from _match(p.left, t.operand, b)
        return
    if op in D810_UNARY:
        ir_op = D810_UNARY[op]
        if isinstance(t, UnOp) and t.op == ir_op:
            yield from _match(p.left, t.operand, b)
        elif isinstance(t, Const):
            yield from _match(p.left, normalize(UnOp(ir_op, t, t.width)), b)
        return
    if op not in D810_BINARY:
        return
    ir_op = D810_BINARY[op]
    m = mask(t.width)
    left_c, right_c = _concrete(p.left), _concrete(p.right)

    # Pattern shapes that normalize() rewrites away in targets.
    if isinstance(t, UnOp) and t.op == "not" and ir_op == "xor":
        for const, other in ((left_c, p.right), (right_c, p.left)):
            if const is not None and const & m == m:
                yield from _match(other, t.operand, b)
    if isinstance(t, UnOp) and t.op == "neg":
        if ir_op == "mul":
            for const, other in ((left_c, p.right), (right_c, p.left)):
                if const is not None and const & m == m:
                    yield from _match(other, t.operand, b)
        if ir_op == "sub" and left_c is not None and left_c & m == 0:
            yield from _match(p.right, t.operand, b)
    if not isinstance(t, BinOp):
        return

    if t.op == ir_op:
        yield from _pairs([(p.left, t.left), (p.right, t.right)], b)
        if ir_op in _COMMUTATIVE:
            yield from _pairs([(p.left, t.right), (p.right, t.left)], b)
    if ir_op == "sub" and t.op == "add":  # A + B == A - (-B)
        yield from _pairs([(p.left, t.left), (p.right, _negate(t.right))], b)
        yield from _pairs([(p.left, t.right), (p.right, _negate(t.left))], b)
    elif ir_op == "add" and t.op == "sub":  # A - B == A + (-B)
        yield from _pairs([(p.left, t.left), (p.right, _negate(t.right))], b)
        yield from _pairs([(p.right, t.left), (p.left, _negate(t.right))], b)
    elif ir_op == "shl" and t.op == "mul" and right_c is not None:
        factor = (1 << right_c) & m
        for const, other in ((t.left, t.right), (t.right, t.left)):
            if isinstance(const, Const) and const.value == factor:
                yield from _match(p.left, other, b)
