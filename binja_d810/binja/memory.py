"""Loads that cannot fault (v3.5). No binaryninja import.

A load from a fixed address inside a readable segment of the image cannot fault and has no effect: the
loader maps the segment, zero-filled tail included, for the life of the process. The range must also lie in
one section that is not external: Binary Ninja's synthetic external section holds no memory.
"""

from __future__ import annotations

_EXTERNAL = "ExternalSectionSemantics"


def fixed_load_is_safe(view, address: int, size: int) -> bool:
    if size <= 0 or address < 0:
        return False
    end = address + size
    segment = view.get_segment_at(address)
    if segment is None or not segment.readable or end > segment.end:
        return False
    return any(s.start <= address and end <= s.end and s.semantics.name != _EXTERNAL
               for s in view.get_sections_at(address))


class FixedLoads:
    """fixed_load_is_safe for one view, remembered by (address, size)."""

    def __init__(self, view):
        self.view = view
        self.known: dict = {}

    def __call__(self, address: int, size: int) -> bool:
        key = (address, size)
        if key not in self.known:
            self.known[key] = fixed_load_is_safe(self.view, address, size)
        return self.known[key]
