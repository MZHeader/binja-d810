"""Configured read-only data (v4.0): const copies of auto variables and byte arrays for gaps. Recorded user
data variables survive Binary Ninja's auto analysis; genuine user types are preserved. Undo uses the record."""

from __future__ import annotations

from ...flow.ranges import layout
from ...flow.record import SETTING, Change
from .. import log
from . import evidence


def release_stale(work, ranges: list[tuple[int, int]]) -> None:
    """The setting no longer claims the range changes it does not list."""
    wanted = set(ranges)
    with work.state.lock:
        for change in work.record.changes():
            if change.kind == "range" and (change.key, change.detail["end"]) not in wanted:
                change.claims.discard(SETTING)
            if change.kind == "table":
                if any(start <= change.key and change.detail["end"] <= end for start, end in ranges):
                    change.claims.add(SETTING)
                else:
                    change.claims.discard(SETTING)


def apply_wanted(work, ranges: list[tuple[int, int]]) -> None:
    """Make each listed range constant, once."""
    for start, end in ranges:
        existing = work.record.get("range", start)
        if existing is None or existing.detail["end"] != end:
            _apply(work, start, end)
        else:
            with work.state.lock:
                existing.detail["users"] = sorted(work.state.enabled)


def _apply(work, start: int, end: int) -> None:
    view = work.view
    # A configured address is not necessarily mapped. Do not manufacture data across an unmapped hole.
    at = start
    while at < end:
        segment = view.get_segment_at(at)
        if segment is None or segment.end <= at:
            log.logger.log_warn(f"flow: read-only range {start:#x}-{end - 1:#x} crosses unmapped data; ignored")
            return
        at = segment.end
    variables = evidence.data_vars(view, start, end)
    inside, gaps = layout(start, end, [(v.address, v.type.width) for v in variables])
    by_address = {v.address: v for v in variables}
    defined, previous = [], []
    readers = evidence.referencing(view, start, end)
    for address, _ in inside:
        var = by_address[address]
        if not var.auto_discovered or evidence.is_const(var.type):
            continue
        t = evidence.const_copy(view, var.type)
        if t is None:
            continue
        previous.append([address, evidence.type_text(var.type)])
        evidence.define_type(view, address, t)
        defined.append([address, evidence.type_text(t)])
    for first, last in gaps:
        t = evidence.const_type(view, 1, last - first)
        if t is not None:
            evidence.define_type(view, first, t)
            defined.append([first, evidence.type_text(t)])
    work.add(Change("range", start, {"end": end, "from": start, "defined": defined,
                                      "readers": sorted(readers), "users": sorted(work.state.enabled)},
                    previous, {SETTING}))
    work.touched |= readers
    log.logger.log_info(f"flow: read-only range {start:#x}-{end - 1:#x}: {len(defined)} variables typed const")
