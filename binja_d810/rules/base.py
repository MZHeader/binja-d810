"""Rule protocol shared by Tier 1 rules and the simplification pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from ..ir.expr import Expr
from ..semantic.verify import Verdict


@dataclass(frozen=True)
class Rewrite:
    original: Expr
    replacement: Expr
    source: str  # rule name, "tier2:<backend>", or "+"-joined sources
    # Set on the final rewrite of an expression: the original -> replacement verification.
    verdict: Optional[Verdict] = None
    method: str = ""  # "signature", "z3" or "sampling"
    reason: str = ""  # why a TESTED verdict has no proof


class Rule(Protocol):
    name: str

    def match(self, e: Expr) -> Optional[Rewrite]: ...
