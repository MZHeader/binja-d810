"""IL-independent per-function fixpoint: plan, verify, apply, report, roll back.

The engine knows nothing about MLIL or HLIL; an ILAdapter (binja_d810.binja.adapter) supplies
candidates, lowering, dead-code analysis, the control-flow fingerprint and the transaction.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import NamedTuple, Sequence

from ..deadline import Deadline
from ..ir.expr import ARITH_OPS, BITWISE_OPS, Const, brief, is_mba, ops, tree_size
from ..semantic import cobra_shim
from ..rules.base import Rewrite, Rule
from ..semantic.constancy import fold_shared
from ..semantic.backend import Tier2Backend
from ..semantic.verify import Verdict
from ..simplify import Limits, Outcome, simplify_expression
from .adapter import ILAdapter
from .storage import visible_size


# Passes planning at once, process-wide. Binary Ninja analyzes functions on parallel threads, and
# Python runs one at a time: with every function's pass planning together, each pass's wall time
# stretched far past its CPU time (27 s for 1 s of work on one large export) and past Binary Ninja's own
# per-function limit. Two slots let one pass plan while another waits on Z3 or CoBRA; waiting for a
# slot counts against the pass's deadline.
PASS_SLOTS = 2
_SLOTS = threading.BoundedSemaphore(PASS_SLOTS)


@dataclass(frozen=True)
class EngineConfig:
    limits: Limits = Limits()
    max_iterations: int = 10
    tier2: bool = True
    disabled_rules: frozenset[str] = frozenset()
    # Shared by both passes of one analysis run. v3.3: 20 s, with Binary Ninja's own
    # per-function limit raised to twice that where D-810 is enabled (binja_d810.binja.limits). At 10 s,
    # an OLLVM-obfuscated export needed 85% of the budget and stopped short on a slower machine.
    function_seconds: float = 20.0


class RewriteRecord(NamedTuple):
    address: int
    source: str
    before: str
    after: str
    verdict: str  # "PROVEN" or "TESTED": the original -> replacement check
    method: str  # "signature", "z3", "low bits" or "sampling"
    reason: str  # why a TESTED rewrite has no proof
    level: str = "mlil"
    mechanism: str = "rules"  # what folded it, for the summary (mechanism())


class SkipRecord(NamedTuple):
    address: int
    reason: str
    level: str = "mlil"
    expression: str = ""  # what was left, in brief


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
    constant_uses: int = 0  # reads replaced by the constant their definition was proven to be (v3.1)
    flow: list[str] = field(default_factory=list)  # targets the flow fixes made constant in this MLIL (v4.0)
    unflatten: list[str] = field(default_factory=list)  # one line per unflattening round (v4.2)
    callees: list[str] = field(default_factory=list)
    callee_calls: int = 0
    scratch: list[str] = field(default_factory=list)
    scratch_stores: int = 0
    scratch_accesses: int = 0
    scratch_aliases: int = 0
    scratch_computations: int = 0
    scratch_seconds: float = 0.0
    flags: list[str] = field(default_factory=list)
    flag_conditions: int = 0
    flag_sources: int = 0
    flag_seconds: float = 0.0
    allocations: list[str] = field(default_factory=list)
    allocation_loads: int = 0
    allocation_lifetimes: int = 0
    allocation_seconds: float = 0.0
    constant_buffers: list[str] = field(default_factory=list)
    constant_regions: int = 0
    constant_steps: int = 0
    constant_strings: list = field(default_factory=list)
    constant_seconds: float = 0.0
    mutable_values: list[str] = field(default_factory=list)
    mutable_domains: list = field(default_factory=list)
    mutable_folds: int = 0
    mutable_seconds: float = 0.0
    control: list[str] = field(default_factory=list)
    control_branches: int = 0
    control_loops: int = 0
    control_seconds: float = 0.0
    preimages: list[str] = field(default_factory=list)
    preimage_conditions: int = 0
    preimage_seconds: float = 0.0
    # Time. Planning is the budgeted work; commit (replace, finalize, SSA, cleanup,
    # rollback) is native and measured separately. overrun: planning past the deadline.
    allowance_seconds: float = 0.0
    budget_exhausted: bool = False
    plan_seconds: float = 0.0
    plan_cpu_seconds: float = 0.0  # this thread's CPU time while planning; much less than plan_seconds: waiting
    commit_seconds: float = 0.0
    lock_wait_seconds: float = 0.0
    slot_wait_seconds: float = 0.0  # waiting for one of PASS_SLOTS
    enumerate_seconds: float = 0.0  # lifting candidates (inside planning)
    slowest: list = field(default_factory=list)  # [(address, seconds, nodes, outcome)], at most 5, slowest first
    overrun_seconds: float = 0.0
    total_seconds: float = 0.0
    reserved_seconds: float = 0.0  # MLIL allowance held for the later control-flow and HLIL passes
    dispatch_probe_seconds: float = 0.0  # included in planning/total: checking for a table dispatcher

    def assurance(self) -> Counter:
        return Counter(r.verdict for r in self.rewrites)

    def summary(self) -> dict:
        """What was folded, by mechanism, and what was left, by reason: zero counts left out."""
        folded = Counter(r.mechanism for r in self.rewrites)
        folded.update({"constant uses": self.constant_uses, "flow": len(self.flow), "unflatten": len(self.unflatten),
                       "callee calls": self.callee_calls, "scratch stores": self.scratch_stores,
                       "scratch accesses": self.scratch_accesses, "flag conditions": self.flag_conditions, "flag sources": self.flag_sources,
                       "allocation loads": self.allocation_loads, "allocation lifetimes": self.allocation_lifetimes,
                       "constant regions": self.constant_regions, "value-set invariants": self.mutable_folds,
                       "equivalent branches": self.control_branches, "dead finite loops": self.control_loops,
                       "comparison preimages": self.preimage_conditions})
        left = Counter(r.reason.split(":")[0] for r in self.skipped)
        return {"folded": {k: n for k, n in folded.items() if n}, "left": dict(left)}


def mechanism(rewrite: Rewrite) -> str:
    """The mechanism a rewrite counts under in the summary."""
    source = rewrite.source
    if rewrite.original.width == 1 and isinstance(rewrite.replacement, Const):
        return "predicates"
    if "constancy" in source:
        return "constants"
    if "select" in source:
        return "conditions"
    if "tier2:bitwise" in source:
        return "bitwise"
    return "mba" if "tier2:" in source else "rules"


def _grows_visible_code(candidate, replacement) -> bool:
    """The size gate (simplify._worth_applying) compares with the lifted form, which inlines
    definitions. When those definitions stay alive, the rewrite only copies them into the root:
    `arg2 - arg1 + rdi_1` (rdi_1 = arg1 & ~0x7f, used elsewhere) became `arg2 - (0x7f & arg1)`. A
    rewrite must not grow what is visible, nor mix arithmetic and bitwise operations where the
    visible code did not."""
    if candidate.consumer_kinds is not None and _newly_mixes(candidate, replacement):
        return True
    if candidate.visible_nodes is None:
        return False
    if visible_size(replacement) > candidate.visible_nodes:
        return True
    return not candidate.visible_mixed and is_mba(replacement)


def _kinds(e) -> frozenset:
    found = ops(e)
    return frozenset({"arith"} if found & ARITH_OPS else set()) | frozenset({"bitwise"} if found & BITWISE_OPS else set())


def _newly_mixes(candidate, replacement) -> bool:
    """The statement HLIL shows the root in (its value's single-use consumers, v3.1) was arithmetic
    or bitwise only and would mix: n + d - 8 with d = -8 | p (glibc's pointer alignment)."""
    both = {"arith", "bitwise"}
    before = candidate.consumer_kinds | candidate.root_kinds
    return not both <= before and both <= (candidate.consumer_kinds | _kinds(replacement))


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


def _passes(adapter, rules, backends, config, deadline, report, dead: set, skip) -> None:
    """Plan, apply and clean up until nothing changes, the iteration cap or the deadline.

    Each pass tries every candidate with the cheap backends (rules and the atom-abstracting linear
    backend) first, and only then the others on what is left: in a Tigress VM's one 248-candidate
    function, CoBRA's slow failures on the first candidates otherwise used up the budget before
    later candidates the linear backend answers in milliseconds were reached.
    """
    level = adapter.level
    cheap = [b for b in backends if getattr(b, "abstracts_atoms", False)]
    rounds = [cheap, list(backends)] if cheap and len(cheap) < len(backends) else [list(backends)]
    # Outcomes of the last round without a rewrite, by lifted expression: a pure function of it
    # (names are symbols), so an unchanged candidate is not planned again in later passes.
    # Outcomes cut short by the shared deadline are not kept.
    stuck: dict = {}
    tier1_cache: dict = {}  # both rounds and every pass ask about the same expressions
    constants: dict = {}  # constancy outcomes of constant-only lifts, by expression (v3.1)
    inconclusive: dict = {}  # proofs that ran out of time, by shape: not tried again in this run (v3.5)
    last_sources: list = []
    for _ in range(config.max_iterations):
        planning, planning_cpu = time.monotonic(), time.thread_time()
        planned = []
        consumed: set = set()
        # Last first (the adapter's order): a later candidate that inlines a single-use definition
        # makes rewriting that definition on its own wasted work (it is dead once the later one is
        # applied). Candidates are lifted lazily, so the deadline also bounds lifting.
        order: dict = {}  # candidate -> position in the adapter's (last first) order
        pending = _numbered(adapter.candidates(skip), order, report)
        by_path: list = []  # last-round candidates left for per-path constancy
        # Constant folds first (v3.3): the lifts only a proven constant may come from, by constancy alone,
        # before any backend runs. An opaque predicate folded now removes the junk code behind it in the
        # next pass (an OLLVM-obfuscated function: Tier 2 on hundreds of junk roots used the whole budget
        # before its predicates' 10-60 ms folds were reached). Constancy rejects ordinary roots after a
        # sample or two.
        rest = []
        for candidate in pending:
            if deadline.expired():
                report.budget_exhausted = True
                break
            rewrite = None
            if candidate.ir not in stuck and (candidate.unchecked is not None or candidate.shared_dag is not None):
                rewrite, _ = _constant_only(candidate, constants, deadline, inconclusive)
            if rewrite is None:
                rest.append(candidate)
                continue
            planned.append((candidate, rewrite))
            consumed |= candidate.consumes
        pending = rest
        # A pass that folded constants commits them before any backend runs: the folded predicates remove
        # the code behind them, so the backends work in the next pass on what remains.
        for number, round_backends in enumerate(rounds if not planned else []):
            final = number == len(rounds) - 1
            deferred = []
            for candidate in pending:
                if candidate.single_use and candidate.defines is not None and candidate.defines in consumed:
                    report.subsumed += 1
                    continue
                if deadline.expired():
                    # No new solver or proof work; candidates already verified are still applied.
                    report.budget_exhausted = True
                    break
                # A candidate stuck in an earlier pass skips the simplification, a function of its lift. Its
                # constant-only and per-path attempts depend on more (the merges around it, v3.3) and run
                # again in the last round, remembered by their own lifts.
                known = stuck.get(candidate.ir)
                if known is not None and not final:
                    deferred.append(candidate)
                    continue
                def attempt(target):
                    result = simplify_expression(target.ir, rules, round_backends, config.limits, frozenset(dead),
                                                 deadline=deadline, tier1_cache=tier1_cache)
                    report.refuted += result.refuted
                    for source, message in result.errors:
                        if source != "verify":
                            dead.add(source)
                        report.errors.append(ErrorRecord(source, message, level))
                    return result

                started = time.monotonic()
                outcome = (Outcome() if candidate.constant_only
                           else Outcome(skipped=known.skipped) if known is not None else attempt(candidate))
                chosen, rewrite, grows = candidate, outcome.rewrite, False
                if rewrite is not None and _grows_visible_code(candidate, rewrite.replacement):
                    rewrite, grows = None, True
                worth = outcome.rewrite is not None or (outcome.skipped or "").startswith("too complex")
                if (known is None and candidate.shared and worth and hasattr(adapter, "shallow")
                        and not deadline.expired()):
                    # The lift inlined definitions that stay alive (values used several times, as in
                    # a Tigress VM's handlers): a result over those shared values may be smaller, and
                    # never copies them. The smaller visible result wins. Only worth a second
                    # simplification when the first found a rewrite (which may copy them) or gave up
                    # on size; most roots of ordinary code simplify neither way.
                    local = adapter.shallow(candidate)
                    if local is not None and local.ir != candidate.ir:
                        found = attempt(local).rewrite
                        if (found is not None and not _grows_visible_code(local, found.replacement)
                                and (rewrite is None
                                     or visible_size(found.replacement) < visible_size(rewrite.replacement))):
                            chosen, rewrite, grows = local, found, False
                if (known is None and grows and candidate.consumes and hasattr(adapter, "shallow")
                        and not deadline.expired()):
                    # The inlined form was refused (larger, or mixing statements that were each
                    # arithmetic or bitwise only): the root alone, over its inputs as they are.
                    local = adapter.shallow(candidate, keep=candidate.consumes)
                    if local is not None and local.ir != candidate.ir:
                        found = attempt(local).rewrite
                        if found is not None and not _grows_visible_code(local, found.replacement):
                            chosen, rewrite, grows = local, found, False
                _note_slowest(report, candidate, time.monotonic() - started, rewrite, outcome)
                if rewrite is None and not grows and final and not deadline.expired():
                    # The constant-only lifts again (remembered from the first stage: no new work unless
                    # it was cut short).
                    rewrite, unproven = _constant_only(candidate, constants, deadline, inconclusive)
                    if unproven:
                        outcome.skipped = "constant on every sample, not proven"
                    if rewrite is None and (candidate.paths is not None or hasattr(adapter, "path_variants")):
                        by_path.append(candidate)  # per-path constancy comes last (below)
                if rewrite is None and not final:
                    deferred.append(candidate)
                    continue
                if outcome.skipped and rewrite is None:
                    skip(candidate.address, outcome.skipped, candidate.ir)
                if outcome.rewrite is None and outcome.skipped != "budget exhausted" and not deadline.expired():
                    stuck[candidate.ir] = outcome
                if grows:
                    skip(candidate.address, "would make the visible code larger or newly mixed", candidate.ir)
                elif rewrite is not None:
                    order[id(chosen)] = order[id(candidate)]
                    planned.append((chosen, rewrite))
                    consumed |= chosen.consumes
            pending = deferred
        # Per-path constancy last, with what remains of the budget (v3.3): the costliest
        # and rarest mechanism must not take the time of other candidates' folds (an OLLVM-obfuscated export).
        for candidate in by_path:
            if deadline.expired():
                report.budget_exhausted = True
                break
            rewrite = _fold_paths(adapter, candidate, constants, deadline, inconclusive)
            if rewrite is not None:
                planned.append((candidate, rewrite))
        now = time.monotonic()
        report.plan_seconds += now - planning
        report.plan_cpu_seconds += time.thread_time() - planning_cpu
        report.overrun_seconds = max(report.overrun_seconds, now - deadline.at)
        if deadline.expired():
            report.budget_exhausted = True
        if not planned:
            return
        committing = time.monotonic()
        dead_before = adapter.dead_assignments()
        planned.sort(key=lambda item: -order[id(item[0])])  # apply in instruction order, as before
        for candidate, rewrite in planned:
            adapter.replace(candidate, rewrite.replacement)
            report.rewrites.append(RewriteRecord(
                candidate.address, rewrite.source, brief(rewrite.original, 2000), brief(rewrite.replacement, 2000),
                rewrite.verdict.name if rewrite.verdict else "UNCHECKED", rewrite.method, rewrite.reason, level,
                mechanism(rewrite)))
        adapter.finish()
        if hasattr(adapter, "propagate_constants"):
            # Definitions made constant: their other reads become the constant (v3.1), so plain uses
            # (a + k * 3, a call's argument) show it and the definitions die.
            folded = [c for c, r in planned if isinstance(r.replacement, Const) and r.verdict is not None
                      and r.verdict.name == "PROVEN"]
            uses = adapter.propagate_constants(folded) if folded else 0
            if uses:
                report.constant_uses += uses
                adapter.finish()
        # Only what these rewrites made dead: ordinary dead stores (z_1 = z at -O0) stay.
        newly_dead = adapter.dead_assignments() - dead_before
        if newly_dead:
            adapter.remove(newly_dead)
            adapter.finish()
        report.commit_seconds += time.monotonic() - committing
        last_sources = sorted({rewrite.source for _, rewrite in planned})
        if report.budget_exhausted:
            return
    report.capped = True
    report.still_firing = last_sources


def _constant_only(candidate, constants: dict, deadline: Deadline, inconclusive: dict):
    """Lifts only a proven constant may come from (v3.1), by constancy alone: the root over inputs whose
    registers were reused before it (x86-64 -O2's x in rdi; arm64 -O2's per-path values), and the root
    too large as a tree but small as shared values (an OLLVM-obfuscated export). A constant reads no variable.
    (rewrite or None, whether a lift was constant on every sample but not proven). Outcomes are
    remembered in `constants` by lift (an outcome cut short by the deadline is not)."""
    unchecked = candidate.unchecked
    if unchecked is not None and not candidate.constant_only and unchecked == candidate.ir:
        unchecked = None
    unproven = False
    for lifted, source in ((unchecked, "constancy (unchecked versions)"),
                           (candidate.shared_dag, "constancy (shared values)")):
        if lifted is None or deadline.expired():
            continue
        if lifted in constants:
            folded = constants[lifted]
        else:
            folded = fold_shared(lifted, deadline, inconclusive=inconclusive)
            if not deadline.expired():
                constants[lifted] = folded
        if folded is not None and folded[1].verdict is Verdict.PROVEN:
            return Rewrite(original=lifted, replacement=Const(folded[0], lifted.width), source=source,
                           verdict=folded[1].verdict, method=folded[1].method, reason=folded[1].reason), False
        unproven = unproven or folded is not None
    return None, unproven


def _fold_paths(adapter, candidate, constants: dict, deadline: Deadline, inconclusive: dict):
    """Per-path constancy: the condition once per incoming path of the merged values
    it reads. The adapter gives alternatives, cheapest first (v3.3: invariant merges alone, then every
    merge split); one folds only if every variant in it is the same proven constant."""
    alternatives = candidate.paths if candidate.paths is not None else adapter.path_variants(candidate)
    for variants in alternatives or ():
        value = _same_constant(variants, constants, deadline, inconclusive)
        if value is not None:
            original = candidate.unchecked if candidate.unchecked is not None else candidate.ir
            return Rewrite(original=original, replacement=Const(value, original.width), source="constancy (per path)",
                           verdict=Verdict.PROVEN, method="z3", reason="")
    return None


def _same_constant(variants, constants: dict, deadline: Deadline, inconclusive: dict):
    """The constant every variant is proven to be, or None."""
    value = None
    for variant in variants:
        if deadline.expired():
            return None
        if variant in constants:
            folded = constants[variant]
        else:
            folded = fold_shared(variant, deadline, inconclusive=inconclusive)
            if not deadline.expired():
                constants[variant] = folded
        if folded is None or folded[1].verdict is not Verdict.PROVEN or value not in (None, folded[0]):
            return None
        value = folded[0]
    return value


def _numbered(candidates, order: dict, report: FunctionReport):
    position = 0
    while True:
        started = time.monotonic()
        try:
            candidate = next(candidates)
        except StopIteration:
            report.enumerate_seconds += time.monotonic() - started
            return
        report.enumerate_seconds += time.monotonic() - started
        order[id(candidate)] = position
        position += 1
        yield candidate


def _note_slowest(report: FunctionReport, candidate, seconds: float, rewrite, outcome) -> None:
    note = "rewrite" if rewrite is not None else (outcome.skipped or "no rewrite")
    report.slowest.append((candidate.address, round(seconds, 3), tree_size(candidate.ir), note))
    report.slowest.sort(key=lambda item: -item[1])
    del report.slowest[5:]


def _run(adapter, rules, backends, config, deadline, report) -> None:
    level = adapter.level
    if deadline.expired():
        report.budget_exhausted = True
        return
    if not adapter.has_candidates():
        return
    waiting = time.monotonic()
    remaining = deadline.remaining()
    acquired = _SLOTS.acquire() if remaining == float("inf") else (remaining > 0 and _SLOTS.acquire(timeout=remaining))
    report.slot_wait_seconds = time.monotonic() - waiting
    if not acquired or deadline.expired():
        if acquired:
            _SLOTS.release()
        report.budget_exhausted = True  # spent waiting for other functions' passes
        return
    try:
        _run_with_slot(adapter, rules, backends, config, deadline, report)
    finally:
        _SLOTS.release()


def _run_with_slot(adapter, rules, backends, config, deadline, report) -> None:
    level = adapter.level
    shape = adapter.cfg_shape()
    dead = set(config.disabled_rules)

    def skip(address: int, reason: str, expression=None) -> None:
        key = (address, reason, level)
        if key not in seen_skips:  # each pass meets the same candidates again
            seen_skips.add(key)
            report.skipped.append(SkipRecord(address, reason, level, "" if expression is None else brief(expression, 400)))

    seen_skips: set = set()
    try:
        _passes(adapter, rules, backends, config, deadline, report, dead, skip)
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
