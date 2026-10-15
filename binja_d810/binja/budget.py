"""The shared per-function allowance across both passes (spec section 8). No binaryninja import.

Accounting is keyed by (view session, function start) and by analysis generation. The generation
token is the address of the MLIL function the MLIL pass worked on: the HLIL activity sees that same
MLIL object within one analysis run (docs/design/spikes/2026-10-15-v2-hlil.md, gate 4), so it
inherits what the MLIL pass left. The ledger keeps a reference to that object, so its address cannot
be reused for another function while the entry lives. Repeated calls in one generation reuse the
entry; a new token (reanalysis) starts a fresh allowance; an HLIL invocation whose MLIL token is
unknown (HLIL regenerated alone) starts its own generation.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Hashable

from ..deadline import Deadline


@dataclass
class _Entry:
    generation: int
    keepalive: object
    allowance: float
    used: dict[str, float] = field(default_factory=lambda: {"mlil": 0.0, "hlil": 0.0})


class Allowance:
    """One pass's share of a generation's allowance. charge() what the pass spent."""

    def __init__(self, ledger: "Ledger", entry: _Entry, level: str, inherited: bool):
        self._ledger, self._entry, self.level, self.inherited = ledger, entry, level, inherited
        self.deadline = Deadline.after(self.remaining())

    def remaining(self) -> float:
        with self._ledger.lock:
            return max(0.0, self._entry.allowance - sum(self._entry.used.values()))

    def used(self) -> float:
        with self._ledger.lock:
            return self._entry.used[self.level]

    def charge(self, seconds: float) -> None:
        with self._ledger.lock:
            self._entry.used[self.level] += seconds


class Ledger:
    def __init__(self, capacity: int = 4096):
        self.lock = threading.Lock()
        self._entries: "OrderedDict[Hashable, _Entry]" = OrderedDict()
        self._capacity = capacity

    def __len__(self) -> int:
        return len(self._entries)

    def begin(self, key: Hashable, level: str, *, generation: int, keepalive: object, allowance: float) -> Allowance:
        with self.lock:
            entry = self._entries.get(key)
            inherited = entry is not None and entry.generation == generation
            if not inherited:
                entry = _Entry(generation, keepalive, allowance)
                self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return Allowance(self, entry, level, inherited and level == "hlil")


LEDGER = Ledger()


def plugin_allowance(function_seconds: float, max_analysis_ms: int) -> float:
    """The shared allowance: the user's budget, but at most half of Binary Ninja's own per-function
    analysis limit, which abandons the whole function (no HLIL) when exceeded. 0 means no limit."""
    if max_analysis_ms <= 0:
        return function_seconds
    return min(function_seconds, max_analysis_ms / 2000)
