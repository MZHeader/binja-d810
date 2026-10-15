"""Expression IR shared by the matcher, rules, Tier 2 backends and the Binary Ninja adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Mapping, Union

BINARY_OPS = frozenset({"add", "sub", "mul", "and", "or", "xor", "shl", "lshr", "ashr"})
UNARY_OPS = frozenset({"neg", "not", "zx", "sx", "trunc"})
SHIFT_OPS = frozenset({"shl", "lshr", "ashr"})
COMMUTATIVE_OPS = frozenset({"add", "mul", "and", "or", "xor"})
ARITH_OPS = frozenset({"add", "sub", "mul", "neg"})
BITWISE_OPS = frozenset({"and", "or", "xor", "not"})


def mask(width: int) -> int:
    return (1 << width) - 1


def to_signed(value: int, width: int) -> int:
    value &= mask(width)
    return value - (1 << width) if value >> (width - 1) else value


@dataclass(frozen=True)
class Var:
    name: str
    width: int
    ref: object = field(default=None, compare=False, hash=False, repr=False)


@dataclass(frozen=True)
class Const:
    value: int
    width: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", self.value & mask(self.width))


@dataclass(frozen=True)
class BinOp:
    op: str
    left: "Expr"
    right: "Expr"
    width: int

    def __post_init__(self) -> None:
        if self.op not in BINARY_OPS:
            raise ValueError(f"unknown binary op {self.op!r}")
        if self.left.width != self.width or (self.op not in SHIFT_OPS and self.right.width != self.width):
            raise ValueError(f"width mismatch in {self.op}: {self.left.width}/{self.right.width} -> {self.width}")


@dataclass(frozen=True)
class UnOp:
    op: str
    operand: "Expr"
    width: int

    def __post_init__(self) -> None:
        if self.op not in UNARY_OPS:
            raise ValueError(f"unknown unary op {self.op!r}")
        inner = self.operand.width
        if self.op in ("neg", "not"):
            ok = inner == self.width
        elif self.op in ("zx", "sx"):
            ok = inner < self.width
        else:  # trunc
            ok = inner > self.width
        if not ok:
            raise ValueError(f"bad widths for {self.op}: {inner} -> {self.width}")


Expr = Union[Var, Const, BinOp, UnOp]


def children(e: Expr) -> tuple[Expr, ...]:
    if isinstance(e, BinOp):
        return (e.left, e.right)
    if isinstance(e, UnOp):
        return (e.operand,)
    return ()


def walk(e: Expr) -> Iterator[Expr]:
    """Pre-order traversal without recursion (lifted trees can be deep)."""
    stack = [e]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(children(node)))


def node_count(e: Expr) -> int:
    return sum(1 for _ in walk(e))


def variables(e: Expr) -> list[Var]:
    seen: dict[tuple[str, int], Var] = {}
    for node in walk(e):
        if isinstance(node, Var):
            seen.setdefault((node.name, node.width), node)
    return list(seen.values())


def ops(e: Expr) -> set[str]:
    return {node.op for node in walk(e) if isinstance(node, (BinOp, UnOp))}


def is_mba(e: Expr) -> bool:
    found = ops(e)
    return bool(found & ARITH_OPS) and bool(found & BITWISE_OPS)


_SYMBOLS = {"add": "+", "sub": "-", "mul": "*", "and": "&", "or": "|", "xor": "^", "shl": "<<", "lshr": ">>"}


def to_str(e: Expr) -> str:
    if isinstance(e, Var):
        return e.name
    if isinstance(e, Const):
        signed = to_signed(e.value, e.width)
        if -4096 <= signed < 0:
            return f"({signed})"
        return str(signed) if 0 <= signed <= 4096 else hex(e.value)
    if isinstance(e, UnOp):
        inner = to_str(e.operand)
        if e.op == "neg":  # keep "-<literal>" for folded negative constants only (see parse)
            return f"(-({inner}))" if inner[0].isdigit() or inner.startswith("(-") else f"(-{inner})"
        if e.op == "not":
            return f"(~{inner})"
        return f"{e.op}{e.width}({inner})"
    if e.op == "ashr":
        return f"sar({to_str(e.left)}, {to_str(e.right)})"
    return f"({to_str(e.left)} {_SYMBOLS[e.op]} {to_str(e.right)})"


def substitute(e: Expr, mapping: Mapping[str, Expr]) -> Expr:
    if isinstance(e, Var):
        return mapping.get(e.name, e)
    if isinstance(e, Const):
        return e
    if isinstance(e, UnOp):
        return UnOp(e.op, substitute(e.operand, mapping), e.width)
    return BinOp(e.op, substitute(e.left, mapping), substitute(e.right, mapping), e.width)
