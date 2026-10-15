"""A monotonic deadline passed through planning, Tier 1, Tier 2, the verifier and the backends."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Deadline:
    at: float  # time.monotonic() value

    @classmethod
    def after(cls, seconds: float) -> "Deadline":
        return cls(time.monotonic() + max(0.0, seconds))

    @classmethod
    def never(cls) -> "Deadline":
        return cls(float("inf"))

    def remaining(self) -> float:
        return max(0.0, self.at - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.at

    def earlier(self, other: "Deadline") -> "Deadline":
        return self if self.at <= other.at else other
