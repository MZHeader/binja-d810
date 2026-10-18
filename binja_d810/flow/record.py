"""What the flow fixes changed, and which functions justify each change (v4.0, spec 3.1). No binaryninja import.

The record is kept in the view's metadata as JSON, so a saved database still knows what to undo when it is
reopened. A change whose claims are all released is reverted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

SETTING = -1  # the claim of the read-only ranges setting (goal 4), in place of a function's start


@dataclass
class Change:
    kind: str  # "table", "range", "function", "split", "inline" or "branches"
    key: int  # where: a table's or range's start, a function's start, a jump's address
    detail: dict = field(default_factory=dict)  # JSON-able, by kind: "end", "defined", "targets", "function", ...
    previous: list = field(default_factory=list)  # what it replaced: [[address, type text], ...]
    claims: set = field(default_factory=set)  # function starts (or SETTING) that need it

    def to_data(self) -> dict:
        return {"kind": self.kind, "key": self.key, "detail": self.detail, "previous": self.previous,
                "claims": sorted(self.claims)}

    @classmethod
    def from_data(cls, data: dict) -> "Change":
        return cls(str(data["kind"]), int(data["key"]), dict(data["detail"]), [list(p) for p in data["previous"]],
                   {int(c) for c in data["claims"]})


class Record:
    def __init__(self, changes=()):
        self._changes: dict[tuple[str, int], Change] = {}
        for change in changes:
            self._changes[(change.kind, change.key)] = change

    @classmethod
    def from_json(cls, text: str) -> "Record":
        return cls(Change.from_data(item) for item in json.loads(text))

    def to_json(self) -> str:
        return json.dumps([c.to_data() for c in self.changes()], sort_keys=True)

    def __len__(self) -> int:
        return len(self._changes)

    def changes(self) -> list[Change]:
        return sorted(self._changes.values(), key=lambda c: (c.kind, c.key))

    def get(self, kind: str, key: int) -> Optional[Change]:
        return self._changes.get((kind, key))

    def add(self, change: Change) -> Change:
        """Keep a change; one already kept for the same kind and key gains its claims instead."""
        existing = self._changes.get((change.kind, change.key))
        if existing is not None:
            existing.claims |= change.claims
            return existing
        self._changes[(change.kind, change.key)] = change
        return change

    def drop(self, kind: str, key: int) -> None:
        self._changes.pop((kind, key), None)

    def claimed_by(self, function: int) -> list[Change]:
        return [c for c in self.changes() if function in c.claims]

    def release(self, function: int) -> None:
        for change in self._changes.values():
            change.claims.discard(function)

    def unclaimed(self) -> list[Change]:
        return [c for c in self.changes() if not c.claims]

    def covering(self, start: int, end: int) -> Optional[Change]:
        """A table or range change holding every byte of [start, end)."""
        for change in self.changes():
            if change.kind in ("table", "range") and change.key <= start and end <= change.detail["end"]:
                return change
        return None

    def overlapping(self, start: int, end: int) -> Optional[Change]:
        """A table or range change holding any byte of [start, end)."""
        for change in self.changes():
            if change.kind in ("table", "range") and change.key < end and start < change.detail["end"]:
                return change
        return None

    def branches_of(self, function: int) -> list[tuple[int, list[int]]]:
        """(jump address, targets) for each jump of `function` with recorded branches."""
        return [(c.key, list(c.detail["targets"])) for c in self.changes()
                if c.kind == "branches" and c.detail["function"] == function]
