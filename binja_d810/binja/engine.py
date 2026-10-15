"""IL-independent per-function fixpoint (spec section 4): plan, verify, apply, report, roll back.

The engine knows nothing about MLIL or HLIL; an ILAdapter (binja_d810.binja.adapter) supplies
candidates, lowering, dead-code analysis, the control-flow fingerprint and the transaction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple, Sequence

from ..ir.expr import to_str
from ..rules.base import Rule
from ..semantic.backend import Tier2Backend
from ..simplify import Limits, simplify_expression
from .adapter import ILAdapter


@dataclass(frozen=True)
class EngineConfig:
    limits: Limits = Limits()
    max_iterations: int = 10
    tier2: bool = True
    disabled_rules: frozenset[str] = frozenset()


class RewriteRecord(NamedTuple):
    address: int
    source: str
    before: str
    after: str
    verdict: str  # "PROVEN" or "TESTED": the original -> replacement check (spec section 10)
    method: str  # "signature", "z3" or "sampling"
    reason: str  # why a TESTED rewrite has no proof
    level: str = "mlil"


class SkipRecord(NamedTuple):
    address: int
    reason: str
    level: str = "mlil"


class ErrorRecord(NamedTuple):
    source: str
    message: str
    level: str = "mlil"


@dataclass
class FunctionReport:
    rewrites: list[RewriteRecord] = field(default_factory=list)
    skipped: list[SkipRecord] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)
    capped: bool = False
    still_firing: list[str] = field(default_factory=list)
    rolled_back: bool = False
    refuted: int = 0  # candidate results the checker refuted; never applied
    rolled_back_rewrites: int = 0  # attempted, then undone; never counted as applied

    def assurance(self) -> Counter:
        return Counter(r.verdict for r in self.rewrites)


def simplify_function(adapter: ILAdapter, rules: Sequence[Rule], backends: Sequence[Tier2Backend],
                      config: EngineConfig) -> FunctionReport:
    level = adapter.level
    report = FunctionReport()
    if not adapter.has_candidates():
        return report
    shape = adapter.cfg_shape()
    dead = set(config.disabled_rules)

    def skip(address: int, reason: str) -> None:
        report.skipped.append(SkipRecord(address, reason, level))

    try:
        for _ in range(config.max_iterations):
            planned = []
            for candidate in adapter.candidates(skip):
                outcome = simplify_expression(candidate.ir, rules, backends, config.limits, frozenset(dead))
                report.refuted += outcome.refuted
                for source, message in outcome.errors:
                    if source != "verify":
                        dead.add(source)
                    report.errors.append(ErrorRecord(source, message, level))
                if outcome.skipped:
                    skip(candidate.address, outcome.skipped)
                if outcome.rewrite is not None:
                    planned.append((candidate, outcome.rewrite))
            if not planned:
                break
            dead_before = adapter.dead_assignments()
            for candidate, rewrite in planned:
                adapter.replace(candidate, rewrite.replacement)
                report.rewrites.append(RewriteRecord(
                    candidate.address, rewrite.source, to_str(rewrite.original), to_str(rewrite.replacement),
                    rewrite.verdict.name if rewrite.verdict else "UNCHECKED", rewrite.method, rewrite.reason, level))
            adapter.finish()
            # Only what these rewrites made dead: ordinary dead stores (z_1 = z at -O0) stay.
            newly_dead = adapter.dead_assignments() - dead_before
            if newly_dead:
                adapter.remove(newly_dead)
                adapter.finish()
            last_sources = sorted({rewrite.source for _, rewrite in planned})
        else:
            report.capped = True
            report.still_firing = last_sources
        if adapter.cfg_shape() != shape:
            raise RuntimeError("control flow changed by expression rewrites")
    except Exception as exc:
        report.errors.append(ErrorRecord("engine", f"{type(exc).__name__}: {exc}", level))
        adapter.restore()
        report.rolled_back_rewrites = len(report.rewrites)
        report.rewrites.clear()
        report.rolled_back = True
    return report
