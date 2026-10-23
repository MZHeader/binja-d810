"""Backward may-read analysis over a finite byte-mask lattice.

A write is dead only when none of its bytes can be read on any outgoing path before being
overwritten. Partial writes kill just their own bytes. Unknown effects are reads of every
possibly observed byte, supplied by the adapter. No control-flow path is guessed away.
"""

from collections import deque
from dataclasses import dataclass

from ..deadline import Deadline


@dataclass(frozen=True)
class Access:
    reads: int = 0
    writes: int = 0
    removable: bool = False


@dataclass(frozen=True)
class Result:
    dead: frozenset[int] = frozenset()
    live_out: dict | None = None
    visits: int = 0
    reason: str = ""


def analyze(accesses: dict[int, Access], successors: dict[int, tuple[int, ...]], *,
            max_visits: int, deadline: Deadline = Deadline.never(), exit_live: int = 0) -> Result:
    if exit_live < 0 or any(a.reads < 0 or a.writes < 0 for a in accesses.values()):
        return Result(reason="invalid byte mask")
    if set(successors) != set(accesses) or any(target not in accesses for row in successors.values() for target in row):
        return Result(reason="incomplete control flow")
    predecessors = {key: set() for key in accesses}
    for source, targets in successors.items():
        for target in targets:
            predecessors[target].add(source)
    incoming = {key: 0 for key in accesses}
    outgoing = {key: 0 for key in accesses}
    pending = deque(reversed(sorted(accesses)))
    queued = set(accesses)
    visits = 0
    while pending:
        if visits >= max_visits or deadline.expired():
            return Result(visits=visits, reason="liveness budget exhausted")
        key = pending.popleft()
        queued.remove(key)
        visits += 1
        live = exit_live if not successors[key] else 0
        for target in successors[key]:
            live |= incoming[target]
        outgoing[key] = live
        access = accesses[key]
        before = access.reads | (live & ~access.writes)
        if before != incoming[key]:
            incoming[key] = before
            for pred in sorted(predecessors[key]):
                if pred not in queued:
                    queued.add(pred)
                    pending.append(pred)
    dead = frozenset(key for key, access in accesses.items()
                     if access.removable and access.writes and not access.writes & outgoing[key])
    return Result(dead, outgoing, visits)
