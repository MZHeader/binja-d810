"""Ordering a parallel copy (v4.3 spec 3.3). A self tail call assigns every parameter at once; written one at a
time, a parameter must not be overwritten while another move still reads it."""

from __future__ import annotations

from typing import Hashable, Optional, Sequence


def sequence(moves: Sequence[tuple[Hashable, frozenset]]) -> Optional[list[int]]:
    """An order of `moves` (destination, what its source reads) in which no move reads a destination an earlier
    move wrote, other than its own. Moves keep their order where they may. None on a cycle."""
    left = list(range(len(moves)))
    order: list[int] = []
    while left:
        for i in left:
            dest = moves[i][0]
            if not any(dest in moves[j][1] for j in left if j != i):
                order.append(i)
                left.remove(i)
                break
        else:
            return None
    return order
