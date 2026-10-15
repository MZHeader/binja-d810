import pytest
from helpers import function_named, lift_return, manifest, open_sample, set_types, signature, strip_versions

from binja_d810.binja import engine, log, workflow
from binja_d810.ir.expr import is_mba
from binja_d810.ir.parse import parse
from binja_d810.rules.registry import load_rules
from binja_d810.semantic.verify import Verdict, check_equivalent

# Binary Ninja leaves this one mixed at -O2 ((x | y) - (x & y): orr, and, sub); the first d810
# sample, x - (1 + ~y), already reads x + y in its HLIL.
ENTRY = next(e for e in manifest() if e["name"] == "mba_d810_Xor_HackersDelightRule_1")


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


def test_dead_assignment_analysis_keeps_every_used_value():
    # Untouched -O0 code: every assignment feeds the return except the spill "z_1 = z".
    entry = next(e for e in manifest() if e["name"] == "mba_d810_Add_HackersDelightRule_2")
    bv = open_sample("mba_O0.dylib", d810=False)
    try:
        set_types(bv, {entry["name"]: signature(entry)})
        mlil = function_named(bv, entry["name"]).mlil
        dead = engine._dead_assignments(mlil)
        assert [str(mlil[i]) for i in sorted(dead)] == ["z_1 = z"]
    finally:
        bv.file.close()


def test_dead_intermediates_are_removed():
    # At -O0 the barriers leave copy chains (x8_1 = x8 ^ x9; var_10 = x8_1; ...) that die once the
    # result is rewritten; without cleanup HLIL kept "var_10_1 = x ^ y".
    entry = next(e for e in manifest() if e["name"] == "mba_d810_Add_HackersDelightRule_2")
    bv = open_sample("mba_O0.dylib", d810=True)
    try:
        set_types(bv, {entry["name"]: signature(entry)})
        hlil = "\n".join(str(i) for i in function_named(bv, entry["name"]).hlil.instructions)
        assert "^" not in hlil and "&" not in hlil, hlil
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
