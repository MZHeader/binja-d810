"""The read-only ranges setting (v4.0, spec 3.6): parsing it, and laying a range over the data variables in it.
No binaryninja import."""

from __future__ import annotations

from typing import Iterable


def parse(texts: Iterable[str]) -> tuple[list[tuple[int, int]], list[str]]:
    """"0xSTART-0xEND" entries (end included) as [start, end) ranges, merged and ascending, and the entries
    that do not parse."""
    found, errors = [], []
    for text in texts:
        try:
            first, last = (int(part.strip(), 16) for part in text.split("-"))
        except ValueError:
            errors.append(text)
            continue
        if not 0 <= first <= last < 1 << 64:
            errors.append(text)
            continue
        found.append((first, last + 1))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(found):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged, errors


def layout(start: int, end: int, variables: Iterable[tuple[int, int]]) -> tuple[list[tuple[int, int]],
                                                                                list[tuple[int, int]]]:
    """The variables (address, width) wholly inside [start, end), and the gaps between variables as
    [start, end). A variable straddling an edge is neither: it is not retyped, and its bytes are not a gap."""
    inside, covered = [], []
    for address, width in sorted(variables):
        width = max(width, 1)
        if address + width <= start or end <= address:
            continue
        covered.append((max(address, start), min(address + width, end)))
        if start <= address and address + width <= end:
            inside.append((address, width))
    gaps, at = [], start
    for first, last in covered:
        if first > at:
            gaps.append((at, first))
        at = max(at, last)
    if at < end:
        gaps.append((at, end))
    return inside, gaps
