"""Decoding a computed jump or call target through the tables it reads (v4.0, spec 3.3). No binaryninja import.

A target is an expression whose leaves are loads: `table[i] - KEY`, `table[i] ^ key`, or an MBA form of either.
Each load has a name in the expression and the addresses it may read: one when its address is constant (or
proven constant), at most MAX_ENTRIES when Binary Ninja bounds an index. At most one load is indexed; the
others (key globals) read one address. Each index value gives one decoded target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..ir.evaluate import evaluate
from ..ir.expr import Expr, variables

MAX_ENTRIES = 512  # d810-ng's cap on a table's entries
Read = Callable[[int, int], Optional[int]]  # (address, size in bytes) -> the unsigned value there, or None


@dataclass(frozen=True)
class Load:
    name: str  # the leaf's name in the target
    addresses: tuple[int, ...]  # what it may read, ascending
    size: int  # bytes
    base: Optional[int] = None  # the constant an indexed address adds the index to (the never-written check)


@dataclass(frozen=True)
class Span:
    start: int
    size: int  # one entry, in bytes
    count: int

    @property
    def end(self) -> int:
        return self.start + self.size * self.count


@dataclass(frozen=True)
class Decoded:
    targets: tuple[int, ...]  # one per index value, in address order
    spans: tuple[Span, ...]  # every table and key global the target reads, in load order


def addresses_from(kind: str, *, value: int = 0, values: Sequence[int] = (),
                   ranges: Sequence[tuple[int, int, int]] = (), width: int = 64,
                   limit: int = MAX_ENTRIES) -> Optional[tuple[int, ...]]:
    """The addresses a Binary Ninja value set allows, ascending; None when it is unbounded or holds more than
    `limit`. `kind` is the value set's type name; `ranges` are (start, end, step) with `end` included."""
    mask = (1 << width) - 1
    if kind in ("ConstantValue", "ConstantPointerValue"):
        return (value & mask,)
    if kind == "InSetOfValues":
        found = {v & mask for v in values}
    elif kind in ("SignedRangeValue", "UnsignedRangeValue"):
        found = set()
        for start, end, step in ranges:
            if step <= 0 or end < start or (end - start) // step >= limit:
                return None
            found.update(v & mask for v in range(start, end + 1, step))
    else:
        return None
    if not found or len(found) > limit:
        return None
    return tuple(sorted(found))


def span_of(load: Load) -> Optional[Span]:
    """The entries a load may read, as one span of its entry size; None when an address is not a whole
    number of entries from the first."""
    first, last = load.addresses[0], load.addresses[-1]
    if any((address - first) % load.size for address in load.addresses):
        return None
    return Span(first, load.size, (last - first) // load.size + 1)


def decode(target: Expr, loads: Sequence[Load], read: Read) -> tuple[Optional[Decoded], str]:
    """(the decoded targets, "") or (None, why not)."""
    if not loads:
        return None, "the target reads no table"
    names = {load.name for load in loads}
    others = sorted({v.name for v in variables(target)} - names)
    if others:
        return None, f"the target reads more than tables: {', '.join(others)}"
    indexed = [load for load in loads if len(load.addresses) > 1]
    if len(indexed) > 1:
        return None, "more than one indexed table"
    spans = []
    for load in loads:
        span = span_of(load)
        if span is None:
            return None, f"the entries at {load.addresses[0]:#x} are not whole entries apart"
        spans.append(span)
    env: dict[str, int] = {}
    for load in loads:
        if len(load.addresses) == 1:
            found = read(load.addresses[0], load.size)
            if found is None:
                return None, f"cannot read {load.addresses[0]:#x}"
            env[load.name] = found
    if not indexed:
        return Decoded((evaluate(target, env),), tuple(spans)), ""
    index, targets = indexed[0], []
    for address in index.addresses:
        found = read(address, index.size)
        if found is None:
            return None, f"cannot read {address:#x}"
        targets.append(evaluate(target, {**env, index.name: found}))
    return Decoded(tuple(targets), tuple(spans)), ""
