"""Expression IR shared by the matcher, rules, Tier 2 backends and the Binary Ninja adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Mapping, Union

# Comparisons: 1-bit results of two operands of equal width (signed ones use two's complement).
CMP_OPS = frozenset({"eq", "ne", "ult", "ule", "ugt", "uge", "slt", "sle", "sgt", "sge"})
BINARY_OPS = frozenset({"add", "sub", "mul", "and", "or", "xor", "shl", "lshr", "ashr", "rol", "ror"}) | CMP_OPS
UNARY_OPS = frozenset({"neg", "not", "zx", "sx", "trunc"})
SHIFT_OPS = frozenset({"shl", "lshr", "ashr"})
ROTATE_OPS = frozenset({"rol", "ror"})  # count modulo the width; neither arithmetic nor bitwise for is_mba
COUNT_OPS = SHIFT_OPS | ROTATE_OPS  # the right operand is a count of its own width
COMMUTATIVE_OPS = frozenset({"add", "mul", "and", "or", "xor"})
ARITH_OPS = frozenset({"add", "sub", "mul", "neg"})
BITWISE_OPS = frozenset({"and", "or", "xor", "not"})


def mask(width: int) -> int:
    return (1 << width) - 1


def to_signed(value: int, width: int) -> int:
    value &= mask(width)
    return value - (1 << width) if value >> (width - 1) else value


def _check_width(width: int) -> None:
    # A 0-bit value equals every other: one would make live conditions provably "constant".
    if width < 1:
        raise ValueError(f"{width}-bit value")


@dataclass(frozen=True)
class Var:
    name: str
    width: int
    ref: object = field(default=None, compare=False, hash=False, repr=False)

    def __post_init__(self) -> None:
        _check_width(self.width)


@dataclass(frozen=True)
class Const:
    value: int
    width: int

    def __post_init__(self) -> None:
        _check_width(self.width)
        object.__setattr__(self, "value", self.value & mask(self.width))


@dataclass(frozen=True)
class BinOp:
    op: str
    left: "Expr"
    right: "Expr"
    width: int

    def __post_init__(self) -> None:
        _check_width(self.width)
        if self.op not in BINARY_OPS:
            raise ValueError(f"unknown binary op {self.op!r}")
        if self.op in CMP_OPS:
            if self.width != 1 or self.left.width != self.right.width:
                raise ValueError(f"comparison {self.op} of {self.left.width}/{self.right.width} bits -> {self.width}")
        elif self.left.width != self.width or (self.op not in COUNT_OPS and self.right.width != self.width):
            raise ValueError(f"width mismatch in {self.op}: {self.left.width}/{self.right.width} -> {self.width}")
        # Children's hashes are cached, so hashing a node is O(1) even when the expression shares
        # values and its tree is exponentially larger (spec v3.1 goal 1).
        object.__setattr__(self, "_hash", hash((self.op, self.left, self.right, self.width)))

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        return _same(self, other) if isinstance(other, (BinOp, UnOp, Var, Const)) else NotImplemented

    def __reduce__(self):
        return BinOp, (self.op, self.left, self.right, self.width)

    def __repr__(self) -> str:
        return _repr(self)  # the cached hash is per process


@dataclass(frozen=True)
class UnOp:
    op: str
    operand: "Expr"
    width: int

    def __post_init__(self) -> None:
        _check_width(self.width)
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
        object.__setattr__(self, "_hash", hash((self.op, self.operand, self.width)))

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        return _same(self, other) if isinstance(other, (BinOp, UnOp, Var, Const)) else NotImplemented

    def __reduce__(self):
        return UnOp, (self.op, self.operand, self.width)

    def __repr__(self) -> str:
        return _repr(self)


def _repr(e: "Expr") -> str:
    """The dataclass repr, or a summary when the tree is large (shared values: assertion messages
    and logs must not expand a 2^30-node tree)."""
    if tree_size(e) > 2000:
        return f"{type(e).__name__}(op={e.op!r}, <{distinct_nodes(e)} nodes, shared values counted once>, width={e.width})"

    def plain(n: "Expr") -> str:
        if isinstance(n, BinOp):
            return f"BinOp(op={n.op!r}, left={plain(n.left)}, right={plain(n.right)}, width={n.width})"
        if isinstance(n, UnOp):
            return f"UnOp(op={n.op!r}, operand={plain(n.operand)}, width={n.width})"
        return repr(n)
    return plain(e)


def _same(a: "Expr", b: "Expr") -> bool:
    """Structural equality, each pair of nodes compared once: linear in the shared nodes."""
    stack, seen = [(a, b)], set()
    while stack:
        x, y = stack.pop()
        if x is y:
            continue
        if type(x) is not type(y):
            return False
        if isinstance(x, (Var, Const)):
            if x != y:
                return False
            continue
        if (id(x), id(y)) in seen:
            continue
        seen.add((id(x), id(y)))
        if x._hash != y._hash or x.op != y.op or x.width != y.width:
            return False
        if isinstance(x, BinOp):
            stack += [(x.left, y.left), (x.right, y.right)]
        else:
            stack.append((x.operand, y.operand))
    return True


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


def tree_size(e: Expr) -> int:
    """node_count, computed once per shared value: exact even when the tree is too large to walk."""
    sizes: dict[int, int] = {}
    stack = [(e, False)]
    while stack:
        node, ready = stack.pop()
        if id(node) in sizes:
            continue
        kids = children(node)
        if ready or not kids:
            sizes[id(node)] = 1 + sum(sizes[id(k)] for k in kids)
        else:
            stack.append((node, True))
            stack.extend((k, False) for k in kids if id(k) not in sizes)
    return sizes[id(e)]


def distinct_nodes(e: Expr) -> int:
    """The nodes of `e` counted once each, however often they are shared."""
    seen, stack = set(), [e]
    while stack:
        node = stack.pop()
        if id(node) not in seen:
            seen.add(id(node))
            stack.extend(children(node))
    return len(seen)


def brief(e: Expr, limit: int = 400) -> str:
    """to_str for reports, bounded: a shared value too large to print as a tree is summarized."""
    if tree_size(e) > 4 * limit:
        head = e.op if isinstance(e, (BinOp, UnOp)) else to_str(e)
        return f"{head}(...) [{distinct_nodes(e)} nodes, shared values counted once; {tree_size(e)} as a tree]"
    text = to_str(e)
    return text if len(text) <= limit else text[:limit] + "..."


def nodes(e: Expr) -> Iterator[Expr]:
    """Each distinct node once, pre-order (first appearance): linear when values are shared."""
    seen, stack = set(), [e]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        stack.extend(reversed(children(node)))


def variables(e: Expr) -> list[Var]:
    seen: dict[tuple[str, int], Var] = {}
    for node in nodes(e):
        if isinstance(node, Var):
            seen.setdefault((node.name, node.width), node)
    return list(seen.values())


def ops(e: Expr) -> set[str]:
    return {node.op for node in nodes(e) if isinstance(node, (BinOp, UnOp))}


def is_mba(e: Expr) -> bool:
    found = ops(e)
    return bool(found & ARITH_OPS) and bool(found & BITWISE_OPS)


_SYMBOLS = {"add": "+", "sub": "-", "mul": "*", "and": "&", "or": "|", "xor": "^", "shl": "<<", "lshr": ">>"}


def to_str(e: Expr) -> str:
    if isinstance(e, Var):
        return e.name
    if isinstance(e, Const):
        if e.width == 1:
            return str(e.value)  # a condition's value: 0 or 1
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
    if e.op in ROTATE_OPS or e.op in CMP_OPS:
        return f"{e.op}({to_str(e.left)}, {to_str(e.right)})"
    return f"({to_str(e.left)} {_SYMBOLS[e.op]} {to_str(e.right)})"


def is_candidate(e: Expr) -> bool:
    """Worth simplifying: an MBA, or a comparison over a bitwise operation or a product of two
    non-constants, or one its form decides (x == x after a copy; zx.d(x.b) u> 0xff): opaque
    predicates, before and after a compiler. Plain comparisons (i < n - 1) are left to Binary Ninja."""
    from .bits import decided_comparison  # bits builds on this module

    if is_mba(e):
        return True
    found = list(nodes(e))
    comparisons = [n for n in found if isinstance(n, BinOp) and n.op in CMP_OPS]
    if not comparisons:
        return False
    if any(decided_comparison(n.op, n.left, n.right) is not None for n in comparisons):
        return True
    return any((isinstance(n, (BinOp, UnOp)) and n.op in BITWISE_OPS)
               or (isinstance(n, BinOp) and n.op == "mul" and not isinstance(n.left, Const)
                   and not isinstance(n.right, Const)) for n in found)


def without_refs(e: Expr) -> Expr:
    """`e` with every leaf's reference dropped, for caches that outlive a binary."""
    if isinstance(e, Var):
        return e if e.ref is None else Var(e.name, e.width)
    if isinstance(e, Const):
        return e
    if isinstance(e, UnOp):
        return UnOp(e.op, without_refs(e.operand), e.width)
    return BinOp(e.op, without_refs(e.left), without_refs(e.right), e.width)


def relink(e: Expr, source: Expr) -> Expr:
    """`e` with each leaf replaced by the leaf of `source` with the same name and width, which
    carries `source`'s reference (Binary Ninja variable or IL expression). Expressions compare
    equal whatever their references, so a cached result computed for an equal expression from
    another pass or function must take the references of the one asked about."""
    leaves = {(v.name, v.width): v for v in variables(source)}

    def visit(node: Expr) -> Expr:
        if isinstance(node, Var):
            return leaves.get((node.name, node.width), node)
        if isinstance(node, Const):
            return node
        if isinstance(node, UnOp):
            return UnOp(node.op, visit(node.operand), node.width)
        return BinOp(node.op, visit(node.left), visit(node.right), node.width)

    return visit(e)


def substitute(e: Expr, mapping: Mapping[str, Expr]) -> Expr:
    if isinstance(e, Var):
        return mapping.get(e.name, e)
    if isinstance(e, Const):
        return e
    if isinstance(e, UnOp):
        return UnOp(e.op, substitute(e.operand, mapping), e.width)
    return BinOp(e.op, substitute(e.left, mapping), substitute(e.right, mapping), e.width)
