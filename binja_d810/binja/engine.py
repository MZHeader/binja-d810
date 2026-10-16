"""IL-independent per-function fixpoint (spec section 4): plan, verify, apply, report, roll back.

The engine knows nothing about MLIL or HLIL; an ILAdapter (binja_d810.binja.adapter) supplies
candidates, lowering, dead-code analysis, the control-flow fingerprint and the transaction.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple, Sequence

from ..deadline import Deadline
from ..ir.expr import is_mba, to_str
from ..semantic import cobra_shim
from ..rules.base import Rule
from ..semantic.backend import Tier2Backend
from ..simplify import Limits, simplify_expression
from .adapter import ILAdapter
from .storage import visible_size


@dataclass(frozen=True)
class EngineConfig:
    limits: Limits = Limits()
    max_iterations: int = 10
    tier2: bool = True
    disabled_rules: frozenset[str] = frozenset()
    function_seconds: float = 5.0  # shared by both passes of one analysis run (spec section 8)


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
    subsumed: int = 0  # candidates skipped because a planned later candidate inlined them
    # Time (spec section 8). Planning is the budgeted work; commit (replace, finalize, SSA, cleanup,
    # rollback) is native and measured separately. overrun: planning past the deadline.
    allowance_seconds: float = 0.0
    budget_exhausted: bool = False
    plan_seconds: float = 0.0
    commit_seconds: float = 0.0
    lock_wait_seconds: float = 0.0
    overrun_seconds: float = 0.0
    total_seconds: float = 0.0

    def assurance(self) -> Counter:
        return Counter(r.verdict for r in self.rewrites)


def _grows_visible_code(candidate, replacement) -> bool:
    """The size gate (simplify._worth_applying) compares with the lifted form, which inlines
    definitions. When those definitions stay alive, the rewrite only copies them into the root:
    `arg2 - arg1 + rdi_1` (rdi_1 = arg1 & ~0x7f, used elsewhere) became `arg2 - (0x7f & arg1)`. A
    rewrite must not grow what is visible, nor mix arithmetic and bitwise operations where the
    visible code did not."""
    if candidate.visible_nodes is None:
        return False
    if visible_size(replacement) > candidate.visible_nodes:
        return True
    return not candidate.visible_mixed and is_mba(replacement)


def simplify_function(adapter: ILAdapter, rules: Sequence[Rule], backends: Sequence[Tier2Backend],
                      config: EngineConfig, deadline: Deadline = Deadline.never()) -> FunctionReport:
    start = time.monotonic()
    cobra_shim.reset_lock_wait()
    report = FunctionReport()
    report.allowance_seconds = deadline.remaining() if deadline.at != float("inf") else config.function_seconds
    try:
        _run(adapter, rules, backends, config, deadline, report)
    finally:
        report.lock_wait_seconds = cobra_shim.lock_wait_seconds()
        report.total_seconds = time.monotonic() - start
    return report


def _run(adapter, rules, backends, config, deadline, report) -> None:
    level = adapter.level
    if deadline.expired():
        report.budget_exhausted = True
        return
    if not adapter.has_candidates():
        return
    shape = adapter.cfg_shape()
    dead = set(config.disabled_rules)
    # Outcomes without a rewrite, by lifted expression: a pure function of it (names are symbols),
    # so an unchanged candidate is not planned again in later iterations. Outcomes cut short by
    # the shared deadline are not kept.
    stuck: dict = {}

    def skip(address: int, reason: str) -> None:
        report.skipped.append(SkipRecord(address, reason, level))

    try:
        for _ in range(config.max_iterations):
            planning = time.monotonic()
            planned = []
            consumed: set = set()
            # Last first (the adapter's order): a later candidate that inlines a single-use definition
            # makes rewriting that definition on its own wasted work (it is dead once the later one is
            # applied). Candidates are lifted lazily, so the deadline also bounds lifting.
            for candidate in adapter.candidates(skip):
                if candidate.single_use and candidate.defines is not None and candidate.defines in consumed:
                    report.subsumed += 1
                    continue
                if deadline.expired():
                    # No new solver or proof work; candidates already verified are still applied.
                    report.budget_exhausted = True
                    break
                if candidate.ir in stuck:
                    continue
                outcome = simplify_expression(candidate.ir, rules, backends, config.limits, frozenset(dead),
                                              deadline=deadline)
                report.refuted += outcome.refuted
                for source, message in outcome.errors:
                    if source != "verify":
                        dead.add(source)
                    report.errors.append(ErrorRecord(source, message, level))
                if outcome.skipped:
                    skip(candidate.address, outcome.skipped)
                if outcome.rewrite is None and outcome.skipped != "budget exhausted" and not deadline.expired():
                    stuck[candidate.ir] = outcome
                if outcome.rewrite is not None and _grows_visible_code(candidate, outcome.rewrite.replacement):
                    skip(candidate.address, "would make the visible code larger or newly mixed")
                elif outcome.rewrite is not None:
                    planned.append((candidate, outcome.rewrite))
                    consumed |= candidate.consumes
            now = time.monotonic()
            report.plan_seconds += now - planning
            report.overrun_seconds = max(report.overrun_seconds, now - deadline.at)
            if deadline.expired():
                report.budget_exhausted = True
            if not planned:
                break
            committing = time.monotonic()
            dead_before = adapter.dead_assignments()
            planned.reverse()  # apply in instruction order, as before
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
            report.commit_seconds += time.monotonic() - committing
            last_sources = sorted({rewrite.source for _, rewrite in planned})
            if report.budget_exhausted:
                break
        else:
            report.capped = True
            report.still_firing = last_sources
        if adapter.cfg_shape() != shape:
            raise RuntimeError("control flow changed by expression rewrites")
    except Exception as exc:
        report.errors.append(ErrorRecord("engine", f"{type(exc).__name__}: {exc}", level))
        undo = time.monotonic()
        adapter.restore()
        report.commit_seconds += time.monotonic() - undo
        report.rolled_back_rewrites = len(report.rewrites)
        report.rewrites.clear()
        report.rolled_back = True
