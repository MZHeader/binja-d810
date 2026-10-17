"""The HLIL pass: what MLIL could not express, rewritten in HLIL, safely."""

import oracle
import pytest
from binaryninja import HighLevelILOperation as H, Settings
from helpers import function_named, manifest, open_sample, set_types, signature

from binja_d810.binja import log, workflow
from binja_d810.binja.hlil import adapter as hlil_adapter

ENTRIES = {e["name"]: e for e in manifest()}
# v1's -O2 register-reuse misses on ARM64.
REUSE = ["mba_linear_001", "mba_linear_002", "mba_linear_006", "mba_linear_013", "mba_linear_030"]
FIXTURE_TYPES = {
    "cf_branch": "uint32_t cf_branch(uint32_t x, uint32_t y, uint32_t c)",
    "cf_loop": "uint32_t cf_loop(uint32_t x, uint32_t y, uint32_t n)",
    "cf_reassign": "uint32_t cf_reassign(uint32_t x, uint32_t y)",
    "hl_call_inline": "uint32_t hl_call_inline(uint32_t x, uint32_t y)",
    "hl_volatile": "uint32_t hl_volatile(uint32_t y)",
    "hl_select": "uint32_t hl_select(uint32_t x, uint32_t y, uint32_t c)",
}


@pytest.fixture(autouse=True)
def _both_passes():
    workflow.set_levels_override(None)
    yield
    workflow.set_levels_override(None)


def _reuse(d810=True):
    bv = open_sample("mba_O2.dylib", d810=d810)
    set_types(bv, {n: signature(ENTRIES[n]) for n in REUSE})
    return bv


def _hlil(func) -> str:
    return "\n".join(map(str, func.hlil.instructions))


def test_register_reuse_samples_are_simplified_and_proven():
    bv = _reuse()
    try:
        for name in REUSE:
            func = function_named(bv, name)
            entry = ENTRIES[name]
            final = oracle.function_result(func)
            assert oracle.compare(final, oracle.reference(entry["expected"], entry["width"]), entry["width"]).kind \
                == "proven", (name, _hlil(func))
            report = log.latest_report(func, "hlil")
            assert report is not None and report.rewrites and not report.rolled_back, report
            assert all(r.level == "hlil" and r.verdict in ("PROVEN", "TESTED") for r in report.rewrites)
            (ret,) = [i for i in func.hlil.instructions if i.operation == H.HLIL_RET]
            assert ret.mlil is not None  # cross-IL mapping kept (spike gate 6)
    finally:
        bv.file.close()


def test_parameters_read_after_register_reuse_are_entry_values():
    bv = _reuse()
    try:
        for name in REUSE:
            func = function_named(bv, name)
            ssa = func.hlil.ssa_form
            (ret,) = [i for i in ssa.instructions if i.operation == H.HLIL_RET]
            stack = list(ret.src)
            while stack:
                node = stack.pop()
                if node.operation == H.HLIL_VAR_SSA and ssa.get_ssa_var_definition(node.var) is None:
                    # Undefined versions are only allowed as parameters' entry values.
                    assert node.var.var in func.parameter_vars and node.var.version == 0, (name, node.var)
                stack.extend(o for o in node.operands if hasattr(o, "operation"))
    finally:
        bv.file.close()


def test_dead_assignments_of_the_rewrite_are_removed():
    bv = _reuse()
    try:
        assert _hlil(function_named(bv, "mba_linear_001")) == "return x + y"
    finally:
        bv.file.close()


@pytest.mark.parametrize("level", ["O0", "O2"])
def test_hlil_pass_alone_never_reads_a_stale_version(level):
    # cf_loop: x10_1 = y ^ x and x11_1 = y & x in the loop, then y is reassigned, and the return
    # combines x10_1 + (x11_1 << 1). Inlining gives x + y over the loop's y, not the y current at
    # the return (the spike's wrong rewrite): the per-variable leaf check must refuse it.
    workflow.set_levels_override({"hlil"})
    bv = open_sample(f"hlil_fixtures_{level}.dylib", d810=True)
    plain = open_sample(f"hlil_fixtures_{level}.dylib", d810=False)
    try:
        set_types(bv, FIXTURE_TYPES)
        set_types(plain, FIXTURE_TYPES)
        returns = lambda f: [str(i) for i in f.hlil.instructions if i.operation == H.HLIL_RET]
        assert returns(function_named(bv, "cf_loop")) == returns(function_named(plain, "cf_loop"))
    finally:
        bv.file.close()
        plain.file.close()


def _walk(e):
    stack = [e]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(o for o in node.operands if hasattr(o, "operation"))


def test_failure_in_the_hlil_pass_restores_hlil(monkeypatch):
    workflow.set_levels_override({"mlil"})
    reference = _reuse()
    workflow.set_levels_override(None)
    calls = {"n": 0}
    real_lower = hlil_adapter.lower

    def flaky_lower(hlil, e, loc):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("injected lowering failure")
        return real_lower(hlil, e, loc)

    monkeypatch.setattr(hlil_adapter, "lower", flaky_lower)
    bv = _reuse()
    try:
        rolled = []
        for name in REUSE:
            func, expected = function_named(bv, name), function_named(reference, name)
            report = log.latest_report(func, "hlil")
            if report is not None and report.rolled_back:
                rolled.append(name)
                assert not report.rewrites
                assert _hlil(func) == _hlil(expected), name
        assert rolled, "expected at least one function to roll back"
    finally:
        bv.file.close()
        reference.file.close()


def test_one_toggle_controls_both_passes():
    bv = _reuse(d810=False)
    try:
        func = function_named(bv, "mba_linear_001")
        assert "^" in _hlil(func) and log.latest_report(func, "hlil") is None
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        bv.update_analysis_and_wait()
        func = function_named(bv, "mba_linear_001")
        assert _hlil(func) == "return x + y"
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, func)
        func.reanalyze()
        bv.update_analysis_and_wait()
        assert "^" in _hlil(function_named(bv, "mba_linear_001"))
    finally:
        bv.file.close()


@pytest.mark.parametrize("level", ["O0", "O2"])
def test_effects_and_control_flow_are_preserved(level):
    bv = open_sample(f"hlil_fixtures_{level}.dylib", d810=True)
    plain = open_sample(f"hlil_fixtures_{level}.dylib", d810=False)
    try:
        set_types(bv, FIXTURE_TYPES)
        set_types(plain, FIXTURE_TYPES)
        groups = {"calls": {H.HLIL_CALL, H.HLIL_TAILCALL}, "derefs": {H.HLIL_DEREF, H.HLIL_DEREF_FIELD},
                  "branches": {H.HLIL_IF, H.HLIL_WHILE, H.HLIL_DO_WHILE, H.HLIL_FOR, H.HLIL_SWITCH}}
        for name in FIXTURE_TYPES:
            for ops in groups.values():
                after = sum(n.operation in ops for s in function_named(bv, name).hlil.instructions for n in _walk(s))
                before = sum(n.operation in ops for s in function_named(plain, name).hlil.instructions for n in _walk(s))
                assert after == before, (name, ops)
        # Volatile reads are never merged: two reads of hl_global stay two.
        assert _hlil(function_named(bv, "hl_volatile")).count("hl_global") == 2
    finally:
        bv.file.close()
        plain.file.close()
