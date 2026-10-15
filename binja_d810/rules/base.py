"""Rule protocol shared by Tier 1 rules and the simplification pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from ..ir.expr import Expr


@dataclass(frozen=True)
class Rewrite:
    original: Expr
    replacement: Expr
    source: str  # rule name, "tier2:<backend>", or "+"-joined sources


class Rule(Protocol):
    name: str

    def match(self, e: Expr) -> Optional[Rewrite]: ...
