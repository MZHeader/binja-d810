"""The graph model unflattening works on (v4.2, spec §3.8): a function's basic blocks with SSA values,
independent of Binary Ninja (binja_d810.binja.unflatten.graph builds one from MLIL).

Keys name SSA values; IR leaves carry a Key as their ref, and a leaf of width w reads the low w bits of its
value. A leaf with any other ref is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence, Union

from ..ir.expr import Expr, Var


@dataclass(frozen=True)
class Key:
    var: int  # the variable's identity (Binary Ninja's identifier)
    version: int


@dataclass(frozen=True)
class Edge:
    source: int
    slot: str  # "goto", "true", "false" or "jump"


@dataclass(frozen=True)
class Step:
    """One non-terminator instruction. `expr` is the value of a pure assignment to `dest`; anything else
    (loads, calls, stores) is impure, and what it writes is unknown."""
    dest: Optional[Key]
    expr: Optional[Expr]
    writes: tuple[Key, ...]
    impure: bool


@dataclass(frozen=True)
class Goto:
    target: int


@dataclass(frozen=True)
class Branch:
    cond: Optional[Expr]
    true: int
    false: int


@dataclass(frozen=True)
class Load:
    address: Expr
    width: int  # bits


@dataclass(frozen=True)
class Jump:
    """An indirect jump: `dest` reads the leaves named in `loads`; `targets` maps a destination to a block."""
    dest: Optional[Expr]
    loads: Mapping[str, Load]
    targets: Mapping[int, int]


@dataclass(frozen=True)
class Stop:
    """A return, a call that does not return, or anything else that ends a path."""


Terminator = Union[Goto, Branch, Jump, Stop]


@dataclass(frozen=True)
class Phi:
    sources: tuple[Key, ...]


@dataclass(frozen=True)
class Assign:
    expr: Expr


@dataclass(frozen=True)
class Unknown:
    pass


Definition = Union[Phi, Assign, Unknown]


class Graph(Protocol):
    entry: int

    def blocks(self) -> Sequence[int]: ...

    def preds(self, block: int) -> Sequence[Edge]: ...

    def phis(self, block: int) -> Sequence[Key]: ...

    def phi_source(self, block: int, pred: int, dest: Key) -> Optional[Key]:
        """The value `dest` takes when `block` is entered from `pred`."""

    def steps(self, block: int) -> Sequence[Step]: ...

    def terminator(self, block: int) -> Terminator: ...

    def definition(self, key: Key) -> Definition: ...

    def def_block(self, key: Key) -> Optional[int]:
        """The block that defines `key`; None when it is defined before the entry (a parameter)."""

    def width(self, key: Key) -> int:
        """The value's width in bits."""

    def read(self, address: int, width: int) -> Optional[int]:
        """`width` bits of constant memory at `address`, else None."""

    def live_expression(self, pred: int, key: Key) -> Optional[Expr]:
        """`key`'s value as an expression of values still readable at the end of `pred`, else None."""


def successors(graph: Graph, block: int) -> list[tuple[int, str]]:
    term = graph.terminator(block)
    if isinstance(term, Goto):
        return [(term.target, "goto")]
    if isinstance(term, Branch):
        return [(term.true, "true"), (term.false, "false")]
    if isinstance(term, Jump):
        return [(b, "jump") for b in dict.fromkeys(term.targets.values())]
    return []


def dominators(graph: Graph) -> dict[int, frozenset[int]]:
    """Each block's dominators (itself included). A block unreachable from the entry has only itself."""
    blocks = list(graph.blocks())
    order, seen, stack = [], {graph.entry}, [graph.entry]
    while stack:
        b = stack.pop()
        order.append(b)
        for s, _ in successors(graph, b):
            if s not in seen:
                seen.add(s)
                stack.append(s)
    everything = frozenset(seen)
    doms = {b: everything for b in seen}
    doms[graph.entry] = frozenset({graph.entry})
    changed = True
    while changed:
        changed = False
        for b in order:
            if b == graph.entry:
                continue
            preds = [e.source for e in graph.preds(b) if e.source in seen]
            new = frozenset.intersection(*(doms[p] for p in preds)) | {b} if preds else frozenset({b})
            if new != doms[b]:
                doms[b], changed = new, True
    for b in blocks:
        doms.setdefault(b, frozenset({b}))
    return doms


def reaches(graph: Graph, source: int, target: int, *, without: tuple[int, int]) -> bool:
    """A path from `source` to `target` (possibly empty) that never takes the edge `without`."""
    seen, stack = {source}, [source]
    while stack:
        b = stack.pop()
        if b == target:
            return True
        for s, _ in successors(graph, b):
            if (b, s) != without and s not in seen:
                seen.add(s)
                stack.append(s)
    return False


def key_of(leaf: Var) -> Optional[Key]:
    return leaf.ref if isinstance(leaf.ref, Key) else None
