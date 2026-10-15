import pytest
from helpers import function_named, lift_return, manifest, open_sample, set_types, signature, strip_versions

from binja_d810.binja import engine, log, workflow
from binja_d810.ir.expr import is_mba
from binja_d810.ir.parse import parse
from binja_d810.rules.registry import load_rules
from binja_d810.semantic.verify import Verdict, check_equivalent

ENTRY = next(e for e in manifest() if e["category"] == "d810")


def _open_typed(d810=True):
    bv = open_sample("mba_O2.dylib", d810=d810)
    set_types(bv, {ENTRY["name"]: signature(ENTRY)})
    return bv, function_named(bv, ENTRY["name"])


def test_activity_rewrites_a_d810_sample():
    bv, func = _open_typed()
    try:
        lifted = strip_versions(lift_return(func))
        assert not is_mba(lifted)
        assert check_equivalent(lifted, parse(ENTRY["expected"], ENTRY["width"]), use_z3=False) is not Verdict.REFUTED
        report = log.latest_report(func)
        assert report is not None and report.rewrites and not report.rolled_back
    finally:
        bv.file.close()


def test_disabled_activity_leaves_code_alone():
    bv, func = _open_typed(d810=False)
    try:
        assert is_mba(strip_versions(lift_return(func)))
    finally:
        bv.file.close()


def test_failing_rule_never_breaks_analysis():
    class Exploding:
        name = "Exploding"

        def match(self, e):
            raise RuntimeError("boom")

    workflow.set_rules_override([Exploding(), *load_rules()])
    try:
        bv, func = _open_typed()
        try:
            report = log.latest_report(func)
            assert any(source == "Exploding" for source, _ in report.errors)
            assert not is_mba(strip_versions(lift_return(func)))
        finally:
            bv.file.close()
    finally:
        workflow.set_rules_override(None)


def test_failure_mid_rewrite_rolls_back(monkeypatch):
    calls = {"n": 0}
    real_lower = engine.lower

    def flaky_lower(mlil, e, loc):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("injected lowering failure")
        return real_lower(mlil, e, loc)

    monkeypatch.setattr(engine, "lower", flaky_lower)
    bv = open_sample("mba_O2.dylib", d810=True)
    try:
        rolled = [log.latest_report(f) for f in bv.functions]
        rolled = [r for r in rolled if r is not None and r.rolled_back]
        assert rolled, "expected at least one function to roll back"
        assert all(not r.rewrites for r in rolled)
    finally:
        bv.file.close()
