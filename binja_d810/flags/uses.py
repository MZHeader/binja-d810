"""Observable-use confinement through SSA phi bookkeeping, including dead phi cycles."""
from dataclasses import dataclass

_MISSING=object()


@dataclass(frozen=True)
class Use:
    index: object
    forwarded: object = None
    description: str = ""


class Confinement:
    def __init__(self, users, allowed, tick=lambda:None):
        self.users,self.allowed,self.tick=users,frozenset(allowed),tick
        self.memo={}

    def external(self, start):
        """Return an observable outside use, or None after checking the complete reachable graph."""
        pending,seen=[start],set()
        while pending:
            self.tick();value=pending.pop()
            if value in seen:continue
            seen.add(value)
            cached=self.memo.get(value,_MISSING)
            if cached is not _MISSING:
                if cached is not None:
                    self.memo[start]=cached;return cached
                continue
            for use in self.users(value):
                self.tick()
                # A phi never observes data itself, even if it appears in an accepted slice.
                # Its output's consumers still have to be checked.
                if use.forwarded is not None:
                    pending.append(use.forwarded)
                elif use.index not in self.allowed:
                    found=(value,use)
                    self.memo[start]=found
                    return found
        # Only a completed search may certify nodes in a cycle. A provisional "seen" node
        # is not a cached success: another edge may still lead to an observable consumer.
        self.memo.update((value,None) for value in seen)
        return None
