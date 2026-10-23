"""Track which bytes of an immutable SSA value can still be observed."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Use:
    reads: int = 0
    follows: object = None
    overwritten: int = 0
    unknown: bool = False


def unobserved(start, written, users, tick):
    pending, seen = [(start, written)], set()
    while pending:
        tick()
        value, wanted = pending.pop()
        if not wanted or (value, wanted) in seen:
            continue
        seen.add((value, wanted))
        for use in users(value):
            tick()
            if use.unknown or use.reads < 0 or use.overwritten < 0 or use.reads & wanted:
                return False
            if use.follows is not None:
                pending.append((use.follows, wanted & ~use.overwritten))
    return True
