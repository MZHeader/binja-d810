"""Synthetic arithmetic and private-register summaries, including conservative frame proofs."""

import pytest
from binaryninja import Settings
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, settle
from flowcases import text

from binja_d810.binja import settings, workflow
from binja_d810.binja.callees.lift import Limits, summarize
from binja_d810.binja.flow import apply
from binja_d810.deadline import Deadline


def ror32(value, count):
    count %= 32
    return ((value >> count) | (value << ((32 - count) % 32))) & 0xffffffff


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    off = open_sample(sample("callees", *request.param), d810=False)
    on = open_sample(sample("callees", *request.param), d810=True)
    settle(on)
    yield request.param, on, off
    on.file.close()
    off.file.close()


def test_constant_chain_matches_independent_model(builds):
    _, on, off = builds
    expected = ((ror32(37, 5) * 29 + 11) & 0xffffffff) ^ 0x623417a9 ^ 0x27
    shown = text(function_named(on, "cal_constant"))
    assert f"{expected:#x}" in shown and "cal_chain(" not in shown, shown
    assert "cal_chain(" in text(function_named(off, "cal_constant"))


def test_nested_summaries_remove_calls(builds):
    _, on, _ = builds
    func = function_named(on, "cal_nested")
    shown = text(func)
    assert not any(f"{name}(" in shown for name in ("cal_chain", "cal_rotate", "cal_mix")), shown
    assert "callees:" in workflow.debug_report_text(func)


@pytest.mark.parametrize("name, value", (("cal_private", ((ror32(37, 4) * 13 + 23) & 0xffffffff) ^ 13),
                                        ("cal_private_chain", ((ror32((ror32(37, 4) * 13 + 23) & 0xffffffff, 24)
                                                                 + 53) & 0xffffffff) ^ 13),
                                        ("cal_frame", 37 ^ 13),
                                        ("cal_overwritten_scratch", (ror32(37, 4) * 13 + 23) & 0xffffffff)))
def test_private_register_and_frame_results(builds, name, value):
    build, on, off = builds
    if build[0] == "i686-pe":
        pytest.skip("private-register fixture targets ARM64 and x86-64; ordinary summaries also test i686")
    shown = text(function_named(on, name))
    assert f"{value:#x}" in shown and "_leaf(" not in shown, shown
    assert "_leaf(" in text(function_named(off, name))


@pytest.mark.parametrize("name", ("cal_live_scratch", "cal_partial_scratch", "cal_escaping_scratch"))
def test_live_partial_and_escaping_slots_keep_the_call(builds, name):
    from binaryninja import LowLevelILOperation as Op
    from binja_d810.binja.callees.rewrite import plan

    build, on, off = builds
    if build[0] == "i686-pe":
        pytest.skip("private-register fixture targets ARM64 and x86-64")
    func = function_named(off, name)
    callee = function_named(off, "cal_private_leaf").start
    calls = [i for i in func.lifted_il.instructions if i.operation == Op.LLIL_CALL
             and i.dest.operation == Op.LLIL_CONST_PTR and i.dest.constant == callee]
    sites, reasons = plan(func.lifted_il, func, Limits(), Deadline.after(3))
    assert calls and all(i.instr_index not in sites for i in calls), reasons
    # Native HLIL already removes the unused private-register call in the escaping case.
    # Check our rejection against lifted IL and preserve its existing final output.
    assert text(function_named(on, name)) == text(func)


@pytest.mark.parametrize("name, callee", (("cal_keeps_write", "cal_write"),
                                         ("cal_keeps_mutable", "cal_mutable"),
                                         ("cal_keeps_loop", "cal_loop")))
def test_effects_writable_loads_and_loops_keep_calls(builds, name, callee):
    _, on, _ = builds
    shown = text(function_named(on, name))
    assert f"{callee}(" in shown, shown


def test_named_limits_and_expired_budget_reject_summaries(builds):
    _, _, off = builds
    func = function_named(off, "cal_mix")
    assert summarize(func, Limits(instructions=1)).summary is None
    assert summarize(func, Limits(nodes=1)).summary is None
    assert summarize(func, deadline=Deadline.after(0)).summary is None


def test_implicit_flag_outputs_are_preserved(builds):
    build, on, _ = builds
    if build[0] == "i686-pe":
        pytest.skip("flag fixture targets ARM64 and x86-64")
    shown = text(function_named(on, "cal_flag_result"))
    assert shown == "return 1", shown


def test_nested_depth_limit_is_honored(builds):
    _, _, off = builds
    result = summarize(function_named(off, "cal_chain"), Limits(depth=1))
    assert result.summary is None and "depth limit" in result.reason


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_feature_disable_restores_calls(build):
    bv = open_sample(sample("callees", *build), d810=True)
    try:
        settle(bv)
        Settings().set_bool(settings.CALLEES, False, bv)
        for func in bv.functions:
            func.reanalyze()
        settle(bv)
        assert "cal_chain(" in text(function_named(bv, "cal_constant"))
    finally:
        bv.file.close()


NATIVE_BUILDS = [b for b in MATRIX if b[1] == "O2" and b[0] != "i686-pe"]


@pytest.mark.parametrize("build", NATIVE_BUILDS, ids=lambda b: "-".join(b))
@pytest.mark.parametrize("disabled", (settings.CALLEES, settings.CALLEE_NATIVE, workflow.ACTIVITY_SETTING))
def test_native_inline_flags_are_undone_by_each_disable(build, disabled):
    bv = open_sample(sample("callees", *build), d810=True, options={settings.CALLEE_NATIVE: True})
    try:
        settle(bv)
        changes = [c for c in apply.record_of(bv).changes() if c.kind == "callee_inline"]
        assert changes
        assert all(bv.get_function_at(c.key).inline_during_analysis.value for c in changes)
        Settings().set_bool(disabled, False, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        assert not [c for c in apply.record_of(bv).changes() if c.kind == "callee_inline"]
        assert all(not bv.get_function_at(c.key).inline_during_analysis.value for c in changes)
    finally:
        bv.file.close()


def test_a_user_no_inline_flag_is_respected_before_and_after_analysis():
    bv = open_sample("callees_O2.dylib", d810=False)
    try:
        callee = function_named(bv, "cal_rotate")
        callee.set_user_inline_during_analysis(False)
        callee.reanalyze()
        settle(bv)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, bv)
        Settings().set_bool(settings.CALLEE_NATIVE, True, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        assert "cal_rotate(" in text(function_named(bv, "cal_chain"))
        assert not callee.inline_during_analysis.value and callee.inline_during_analysis.confidence
        changes = [c for c in apply.record_of(bv).changes() if c.kind == "callee_inline"]
        assert changes
        overridden = bv.get_function_at(changes[0].key)
        overridden.set_user_inline_during_analysis(False)
        overridden.reanalyze()
        settle(bv)
        Settings().set_bool(settings.CALLEES, False, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        assert not overridden.inline_during_analysis.value and overridden.inline_during_analysis.confidence
    finally:
        bv.file.close()


@pytest.mark.parametrize("available_after_retry", (True, False))
def test_missing_callee_snapshots_get_only_one_deferred_retry(monkeypatch, available_after_retry):
    from binaryninja import Function

    bv = open_sample("callees_O2.dylib", d810=False)
    try:
        func = function_named(bv, "cal_constant")
        target = function_named(bv, "cal_rotate").start
        prop = Function.lifted_il_if_available
        reads, deferred = [], []

        def snapshot(f):
            if f.start == target:
                reads.append(f.start)
                if len(reads) == 1 or not available_after_retry:
                    return None
            return prop.fget(f)

        original = apply.defer_reanalysis
        def defer(f):
            deferred.append(f.start)
            original(f)
        monkeypatch.setattr(Function, "lifted_il_if_available", property(snapshot))
        monkeypatch.setattr(apply, "defer_reanalysis", defer)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        assert deferred == [func.start]
        assert ("cal_chain(" not in text(func)) == available_after_retry
        assert not apply.busy(bv)
    finally:
        bv.file.close()


@pytest.mark.parametrize("key", (settings.CALLEE_INSTRUCTIONS, settings.CALLEE_NODES, settings.CALLEE_DEPTH,
                                 settings.CALLEE_CALLER_INSTRUCTIONS))
def test_registered_limits_can_prevent_substitution(key):
    # Keep the callees unmodified: a helper already substituted during its own analysis
    # is a flatter current IL body, so a later caller legitimately has less nesting to inspect.
    bv = open_sample("callees_O2.dylib", d810=False, options={key: 1})
    try:
        func = function_named(bv, "cal_constant")
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        assert "cal_chain(" in text(func)
        assert "limit" in workflow.debug_report_text(func)
    finally:
        bv.file.close()


@pytest.mark.parametrize("build", NATIVE_BUILDS, ids=lambda b: "-".join(b))
def test_native_inlining_itself_handles_the_caller_frame(build):
    from binaryninja import InlineDuringAnalysisWithConfidence

    bv = open_sample(sample("callees", *build), d810=False)
    try:
        callees = [function_named(bv, n) for n in ("cal_private_leaf", "cal_private_second", "cal_frame_leaf")]
        previous = [(f, f.inline_during_analysis) for f in callees]
        for f in callees:
            f.set_user_inline_during_analysis(True)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        assert "return 0x28" in text(function_named(bv, "cal_frame"))
        shown = text(function_named(bv, "cal_private_chain"))
        expected = ((ror32((ror32(37, 4) * 13 + 23) & 0xffffffff, 24) + 53) & 0xffffffff) ^ 13
        assert f"{expected:#x}" in shown and "_leaf(" not in shown and "_second(" not in shown
        for f, flag in previous:
            f.set_user_inline_during_analysis(InlineDuringAnalysisWithConfidence(flag.value, flag.confidence))
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        assert "cal_frame_leaf(" in text(function_named(bv, "cal_frame"))
    finally:
        bv.file.close()


def test_native_inline_edit_is_in_the_binary_views_undo_history():
    bv = open_sample("callees_O2.dylib", d810=True, options={settings.CALLEE_NATIVE: True})
    try:
        settle(bv)
        changes = [c for c in apply.record_of(bv).changes() if c.kind == "callee_inline"]
        assert changes and all(bv.get_function_at(c.key).inline_during_analysis.value for c in changes)
        bv.undo()
        settle(bv)
        assert any(not bv.get_function_at(c.key).inline_during_analysis.value for c in changes)
    finally:
        bv.file.close()


def test_a_call_left_by_native_inlining_uses_checked_substitution(monkeypatch):
    from binaryninja import Function, InlineDuringAnalysis, InlineDuringAnalysisWithConfidence

    bv = open_sample("callees_O2.dylib", d810=False)
    try:
        caller = function_named(bv, "cal_constant")
        callee = function_named(bv, "cal_chain")
        original = Function.inline_during_analysis
        def flag(f):
            if f.start == callee.start:
                return InlineDuringAnalysisWithConfidence(InlineDuringAnalysis.InlinePreservingTargetInstructionAddresses)
            return original.fget(f)
        # Report the flag without changing the core's behavior: the call really remains in
        # lifted IL, reproducing a native refusal independently of the current native inliner.
        monkeypatch.setattr(Function, "inline_during_analysis", property(flag))
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, caller)
        caller.reanalyze()
        settle(bv)
        assert "cal_chain(" not in text(caller)
        assert "native inline flag left call" in workflow.debug_report_text(caller)
    finally:
        bv.file.close()


def test_early_work_uses_the_shared_budget_and_expiry_keeps_original_calls(monkeypatch):
    import time
    from binja_d810.binja import log
    from binja_d810.binja.callees import activity

    bv = open_sample("callees_O2.dylib", d810=False)
    try:
        func = function_named(bv, "cal_constant")
        original = activity.plan
        def delayed(il, f, limits, deadline):
            result = original(il, f, limits, deadline)
            time.sleep(1.05)  # only this enabled function, deliberately exhaust its one-second allowance
            return result
        monkeypatch.setattr(activity, "plan", delayed)
        Settings().set_integer(settings.FUNCTION_SECONDS, 1, func)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        mlil = log.latest_reports(func)["mlil"]
        assert mlil.budget_exhausted and mlil.total_seconds >= 1
        assert "cal_chain(" in text(func)
        assert any("budget exhausted" in n for n in mlil.callees)
        assert not mlil.errors
    finally:
        bv.file.close()
