"""Tier 2 backend protocol and the default backend list."""

from __future__ import annotations

from typing import Optional, Protocol

from ..deadline import Deadline
from ..ir.expr import Expr


class Tier2Backend(Protocol):
    name: str

    def simplify(self, e: Expr, deadline: Optional[Deadline] = None) -> Optional[Expr]:
        """A simpler equivalent of `e`, or None. Work must stop soon after `deadline`."""


def default_backends() -> list[Tier2Backend]:
    from .cobra_backend import CobraBackend
    from .cobra_shim import load
    from .linear_mba import LinearMBABackend

    backends: list[Tier2Backend] = []
    solve = load()
    if solve is not None:
        backends.append(CobraBackend(solve))
    backends.append(LinearMBABackend())
    return backends
