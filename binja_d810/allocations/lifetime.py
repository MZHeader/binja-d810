"""Exact byte forwarding for one proven non-escaping, straight-line object lifetime."""

from dataclasses import dataclass, field

from ..deadline import Deadline
from ..ir.expr import Expr
from ..summaries.frame import Frame
from .contracts import Contract


@dataclass(frozen=True)
class Event:
    kind: str
    key: object
    offset: int = 0
    size: int = 0
    value: Expr | None = None


@dataclass
class Plan:
    loads: dict = field(default_factory=dict)
    elide: bool = False
    notes: list[str] = field(default_factory=list)


def prove(events, size: int, allocation: Contract, release: Contract | None, *,
          max_bytes: int, max_events: int, little_endian=True, deadline=Deadline.never()):
    result = Plan()
    if allocation.role != "allocate" or size <= 0 or size > max_bytes:
        return Plan(notes=["unknown or excessive allocation size"])
    if not allocation.nonnull:
        return Plan(notes=["allocation failure is possible; original memory accesses retained"])
    memory = Frame(max_bytes, little_endian)
    freed, complete = False, True
    seen = set()
    try:
        for index, event in enumerate(events):
            if index >= max_events or deadline.expired():
                raise ValueError("allocation lifetime budget exhausted")
            if event.key in seen:
                raise ValueError("allocation event identity is ambiguous")
            seen.add(event.key)
            if freed:
                raise ValueError("object used after release")
            if event.kind == "free":
                if release is None or release.role != "deallocate":
                    raise ValueError("deallocation contract unavailable")
                freed = True
                continue
            if event.kind not in ("store", "load"):
                raise ValueError("object escapes or has an unmodeled effect")
            if event.offset < 0 or event.size <= 0 or event.offset + event.size > size:
                raise ValueError("object access outside the proven allocation")
            if event.kind == "store":
                if event.value is None or event.value.width != event.size * 8:
                    raise ValueError("store value width is unproved")
                memory.write(event.offset, event.value)
            elif all(at in memory.written for at in range(event.offset, event.offset + event.size)):
                result.loads[event.key] = memory.read(event.offset, event.size)
            else:
                complete = False
                result.notes.append("load has bytes without a dominating store")
        result.elide = bool(complete and freed and allocation.nonnull and allocation.elidable and release.elidable)
        if not result.elide:
            result.notes.append("allocation, stores and release retained: failure or external effects are not proved removable")
        return result
    except ValueError as exc:
        return Plan(notes=[str(exc)])  # An eventual escape invalidates earlier forwarding too.
