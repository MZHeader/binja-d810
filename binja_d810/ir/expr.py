"""Expression IR shared by the matcher, rules, Tier 2 backends and the Binary Ninja adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Mapping, Union

# Comparisons: 1-bit results of two operands of equal width (signed ones use two's complement).
CMP_OPS = frozenset({"eq", "ne", "ult", "ule", "ugt", "uge", "slt", "sle", "sgt", "sge"})
# Total division (ARM64's semantics): x / 0 = 0, x % 0 = x, MIN / -1 = MIN, MIN % -1 = 0;
# signed rounds toward zero. Lifters build one only after proving it cannot trap. Neither arithmetic
# nor bitwise for is_mba.
DIV_OPS = frozenset({"udiv", "urem", "sdiv", "srem"})
BINARY_OPS = frozenset({"add", "sub", "mul", "and", "or", "xor", "shl", "lshr", "ashr", "rol", "ror"}) | CMP_OPS | DIV_OPS
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
        # values and its tree is exponentially larger.
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
    """to_str for reports, bounded: its first `limit` characters, rendered no further (v3.3), and a shared
    value far too large to print as a tree summarized."""
    size = tree_size(e)
    if size > 4 * limit and distinct_nodes(e) < size:
        head = e.op if isinstance(e, (BinOp, UnOp)) else to_str(e)
        return f"{head}(...) [{distinct_nodes(e)} nodes, shared values counted once; {size} as a tree]"
    out: list[str] = []
    try:
        _render(e, out, [limit + 1])
    except _Shown:
        return "".join(out)[:limit] + "..."
    return "".join(out)


class _Shown(Exception):
    """brief has rendered more than it shows."""


def _render(e: Expr, out: list[str], room: list[int]) -> None:
    """to_str(e), appended to `out` piece by piece, stopping (_Shown) once `room` characters are written."""
    def put(text: str) -> None:
        out.append(text)
        room[0] -= len(text)
        if room[0] <= 0:
            raise _Shown

    if isinstance(e, (Var, Const)):
        put(to_str(e))
    elif isinstance(e, UnOp):
        if e.op == "neg":  # as to_str: "(-(...))" around a literal or a negation, else "(-...)"
            inner = e.operand
            literal = (isinstance(inner, Const) or (isinstance(inner, UnOp) and inner.op == "neg")
                       or (isinstance(inner, Var) and inner.name[:1].isdigit()))
            put("(-(" if literal else "(-")
            _render(inner, out, room)
            put("))" if literal else ")")
        elif e.op == "not":
            put("(~")
            _render(e.operand, out, room)
            put(")")
        else:
            put(f"{e.op}{e.width}(")
            _render(e.operand, out, room)
            put(")")
    elif e.op == "ashr" or e.op in ROTATE_OPS or e.op in CMP_OPS or e.op in DIV_OPS:
        put(f"{'sar' if e.op == 'ashr' else e.op}(")
        _render(e.left, out, room)
        put(", ")
        _render(e.right, out, room)
        put(")")
    else:
        put("(")
        _render(e.left, out, room)
        put(f" {_SYMBOLS[e.op]} ")
        _render(e.right, out, room)
        put(")")


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


def shape_key(*exprs: Expr) -> tuple:
    """The expressions with their variables renamed v0, v1, ... in order of first appearance (pre-order,
    across the expressions in turn), widths kept: equal exactly for expressions that differ only in
    their variables' names. A proof's verdict is invariant under such renaming.
    Shared values stay shared, and the leaves carry no reference."""
    names: dict[tuple[str, int], str] = {}
    for e in exprs:
        for node in nodes(e):
            if isinstance(node, Var):
                names.setdefault((node.name, node.width), f"v{len(names)}")
    built: dict[int, Expr] = {}
    out = []
    for e in exprs:
        stack = [(e, False)]
        while stack:
            node, ready = stack.pop()
            if id(node) in built:
                continue
            if isinstance(node, Var):
                built[id(node)] = Var(names[(node.name, node.width)], node.width)
            elif isinstance(node, Const):
                built[id(node)] = node
            elif not ready:
                stack.append((node, True))
                stack.extend((k, False) for k in children(node) if id(k) not in built)
            elif isinstance(node, UnOp):
                built[id(node)] = UnOp(node.op, built[id(node.operand)], node.width)
            else:
                built[id(node)] = BinOp(node.op, built[id(node.left)], built[id(node.right)], node.width)
        out.append(built[id(e)])
    return tuple(out)


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
    if e.op in ROTATE_OPS or e.op in CMP_OPS or e.op in DIV_OPS:
        return f"{e.op}({to_str(e.left)}, {to_str(e.right)})"
    return f"({to_str(e.left)} {_SYMBOLS[e.op]} {to_str(e.right)})"


_JUNK_UNARY = frozenset({"not", "zx", "sx", "trunc"})


def is_bitwise_junk(e: Expr) -> bool:
    """A bitwise-only expression shaped like instruction substitution (OLLVM, Hikari, Pluto):
    (a & b) | (a ^ b) for a | b, (a ^ ~b) & a for a & b. Only and, or, xor, not, extensions and
    shifts by constants; at least two and/or/xor; and a variable that occurs twice or a constant
    operand. Plain masks (x & 0xff), combinations (a | b) and hash steps ((h >> 16) ^ h) are not."""
    combining, admitted, names, shared = 0, False, set(), set()
    for node in nodes(e):
        if isinstance(node, UnOp) and node.op not in _JUNK_UNARY:
            return False
        if isinstance(node, BinOp):
            if node.op in SHIFT_OPS:
                if not isinstance(node.right, Const):
                    return False
            elif node.op in ("and", "or", "xor"):
                combining += 1
                admitted = admitted or isinstance(node.left, Const) or isinstance(node.right, Const)
            else:
                return False
        for child in children(node):
            if isinstance(child, Var):
                if child.name in names:
                    admitted = True
                names.add(child.name)
            elif not isinstance(child, Const):
                if id(child) in shared:
                    admitted = True  # a shared subtree: its variables occur twice
                shared.add(id(child))
    return combining >= 2 and admitted


def is_candidate(e: Expr) -> bool:
    """Worth simplifying: an MBA, bitwise substitution junk, or a comparison over a bitwise
    operation or a product of two non-constants, or one its form decides (x == x after a copy;
    zx.d(x.b) u> 0xff): opaque predicates, before and after a compiler. Plain comparisons
    (i < n - 1) are left to Binary Ninja."""
    from .bits import decided_comparison  # bits builds on this module

    if is_mba(e) or is_bitwise_junk(e):
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
