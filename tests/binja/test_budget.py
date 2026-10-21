"""The shared per-function time budget in Binary Ninja."""

import os
import time
from pathlib import Path

import pytest
from binaryninja import Settings
from helpers import function_named, manifest, open_sample, set_types, signature, settle

from binja_d810.binja import log, settings, workflow
from binja_d810.binja.engine import FunctionReport

from binja_d810.semantic import cobra_shim
from binja_d810.semantic.cobra_backend import CobraBackend

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test"))

ENTRIES = {e["name"]: e for e in manifest()}
# Many candidates in MLIL, and a register-reuse return that only the HLIL pass can rewrite.
NAME = "mba_linear_001"
OVERSHOOT = 0.6  # one slow backend call (0.45 s) past the deadline, plus verification sampling
# With many functions analyzed on parallel threads, a thread can also wake late from its lock
# timeout and wait for Python's GIL: wall-clock scheduling slack the plugin cannot preempt.
SCHEDULING = 0.5


class Stall:
    """Takes 0.45 s per call and never helps: Tier 2 keeps trying subtrees until time runs out."""

    name = "stall"

    def simplify(self, e, deadline=None):
        time.sleep(0.45)
        return None


@pytest.fixture(autouse=True)
def _restore():
    yield
    workflow.set_backends_override(None)
    workflow.set_rules_override(None)
    Settings().reset(settings.FUNCTION_SECONDS)


def _open(seconds=None, file_name="mba_O2.dylib"):
    if seconds is not None:
        Settings().set_integer(settings.FUNCTION_SECONDS, seconds)
    bv = open_sample(file_name, d810=True)
    set_types(bv, {NAME: signature(ENTRIES[NAME])})
    return bv, function_named(bv, NAME)


def test_both_passes_share_one_allowance():
    bv, func = _open()
    try:
        reports = log.latest_reports(func)
        mlil, hlil = reports["mlil"], reports["hlil"]
        assert 19.9 < mlil.allowance_seconds <= 20.0  # the default budget (room made in the binary's limit)
        assert abs(hlil.allowance_seconds - (mlil.allowance_seconds - mlil.total_seconds)) < 0.05
    finally:
        bv.file.close()


@pytest.mark.parametrize("unflatten_enabled", [True, False])
def test_a_dispatcher_gets_time_after_mlil_uses_its_allowance(monkeypatch, unflatten_enabled):
    bv = open_sample("flatten_x86_64-elf_O2.so", d810=False,
                     options={settings.FUNCTION_SECONDS: 4, settings.UNFLATTEN: unflatten_enabled})
    original = workflow.simplify_function
    func = function_named(bv, "fl_dense")

    def consume(adapter, rules, backends, config, deadline):
        if adapter.level == "mlil" and adapter.mlil.source_function.start == func.start:
            allowed = deadline.remaining()
            started = time.monotonic()
            time.sleep(allowed + 0.01)  # stand in for an unsuccessful, budget-consuming MBA pass
            elapsed = time.monotonic() - started
            return FunctionReport(allowance_seconds=allowed, total_seconds=elapsed,
                                  plan_seconds=elapsed, budget_exhausted=True)
        return original(adapter, rules, backends, config, deadline)

    monkeypatch.setattr(workflow, "simplify_function", consume)
    try:
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        report = log.latest_report(func, "mlil")
        assert report is not None
        if unflatten_enabled:
            assert report.unflatten, log.render(report)
            assert report.allowance_seconds < 3.1
            assert "switch" not in "\n".join(map(str, func.hlil.instructions))
        else:
            assert not report.unflatten and report.allowance_seconds > 3.9, log.render(report)
    finally:
        bv.file.close()


def test_slow_backend_exhausts_the_shared_budget():
    workflow.set_rules_override([])
    workflow.set_backends_override([Stall()])
    bv, func = _open(seconds=1)
    try:
        reports = log.latest_reports(func)
        planning = sum(r.plan_seconds for r in reports.values())
        detail = {level: log.render(r) for level, r in reports.items()}
        assert any(r.budget_exhausted for r in reports.values()), detail
        assert planning <= 1.0 + OVERSHOOT, planning
        assert all(not r.rolled_back for r in reports.values())
    finally:
        bv.file.close()


def test_reanalysis_gets_a_fresh_allowance():
    workflow.set_rules_override([])
    workflow.set_backends_override([Stall()])
    bv, func = _open(seconds=1)
    try:
        assert any(r.budget_exhausted for r in log.latest_reports(func).values())
        func.reanalyze()
        bv.update_analysis_and_wait()
        report = log.latest_report(function_named(bv, NAME), "mlil")
        assert report.allowance_seconds > 0.95
    finally:
        bv.file.close()


def test_two_views_of_one_file_have_separate_allowances():
    workflow.set_rules_override([])
    workflow.set_backends_override([Stall()])
    first, f1 = _open(seconds=1)
    second, f2 = _open(seconds=1)
    try:
        r1, r2 = log.latest_report(f1, "mlil"), log.latest_report(f2, "mlil")
        assert r1 is not r2
        assert r1.allowance_seconds > 0.95 and r2.allowance_seconds > 0.95
    finally:
        first.file.close()
        second.file.close()


def test_solver_lock_contention_is_bounded_by_each_functions_deadline():
    # Every function of the corpus analyzed in parallel, all through one slow "CoBRA" holding the
    # real solver lock for 0.4 s per call: waiting for it counts against each function's allowance.
    def slow_solve(text, width, deadline=None):
        if not cobra_shim._acquire(deadline):
            return None
        try:
            time.sleep(0.4)
            return None
        finally:
            cobra_shim._SOLVE_LOCK.release()

    workflow.set_rules_override([])
    workflow.set_backends_override([CobraBackend(slow_solve)])
    Settings().set_integer(settings.FUNCTION_SECONDS, 1)
    entries = list(ENTRIES.values())[:24]
    bv = open_sample("mba_O2.dylib", d810=True)
    try:
        set_types(bv, {e["name"]: signature(e) for e in entries})
        reports = [r for e in entries for r in log.latest_reports(function_named(bv, e["name"])).values()]
        assert reports
        worst = max(reports, key=lambda r: r.plan_seconds)
        assert worst.plan_seconds <= 1.0 + 0.4 + SCHEDULING, log.render(worst)  # 0.4 s: one held call
        (OUT / "budget_contention.txt").write_text(log.render(worst))
        assert any(r.lock_wait_seconds > 0 for r in reports)
    finally:
        bv.file.close()


def test_enumeration_stops_at_the_deadline():
    # One large export's roots took 60 s to lift (thousands of roots, none an MBA), and the
    # engine only checked the deadline between candidates it was given, so Binary Ninja abandoned
    # 162 functions. The adapters check the deadline themselves, per instruction.
    from binja_d810.binja.hlil.adapter import HLILAdapter
    from binja_d810.binja.mlil.adapter import MLILAdapter
    from binja_d810.deadline import Deadline

    bv = open_sample("mba_O0.dylib", d810=False)  # -O0: candidates in both MLIL and HLIL
    try:
        set_types(bv, {NAME: signature(ENTRIES[NAME])})
        func = function_named(bv, NAME)
        for make, il in ((MLILAdapter, func.mlil), (HLILAdapter, func.hlil)):
            assert list(make(il, 200).candidates(lambda a, r: None)), make.__name__
            late = make(il, 200, Deadline.after(0))
            assert not late.has_candidates() and list(late.candidates(lambda a, r: None)) == [], make.__name__
    finally:
        bv.file.close()
