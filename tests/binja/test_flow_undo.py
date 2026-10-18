"""v4.0: undoing the flow fixes (binja_d810.binja.flow.undo): the record reaches a fixpoint, and turning D-810
off restores the D-810-off HLIL, through shared claims, a saved database and the user's own edits."""

import binaryninja
import pytest
from arch import IDS, MATRIX, sample
from binaryninja import Settings
from flowcases import CALLS, JUMPS, NEGATIVES, decoded, text
from helpers import data_pointers, function_named, open_sample, settle

from binja_d810.binja import settings, workflow
from binja_d810.binja.flow import apply

FIXTURE = sorted(set(JUMPS) | set(CALLS) | set(NEGATIVES)
                 | {"ident_call", "ident", "f_a", "f_b", "key_read_unused", "escaped_offset"})
BUILD = ("arm64", "O2")


def _texts(bv) -> dict[str, str]:
    return {name: text(function_named(bv, name)) for name in FIXTURE}


def _no_record(bv) -> bool:
    try:
        bv.query_metadata(apply.KEY)
    except KeyError:
        return len(apply.record_of(bv)) == 0
    return False


def _disable(bv, func) -> None:
    Settings().set_bool(workflow.ACTIVITY_SETTING, False, func)
    func.reanalyze()
    settle(bv)


def _disable_all(bv) -> None:
    Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
    for func in list(bv.functions):
        func.reanalyze()
    settle(bv)


def _fresh():
    bv = open_sample(sample("flow", *BUILD), d810=True)
    settle(bv)
    return bv


def _table_of(bv, name: str):
    """The table change the function claims."""
    start = function_named(bv, name).start
    return next(c for c in apply.record_of(bv).changes() if c.kind == "table" and start in c.claims)


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_a_fixpoint_then_turning_d810_off_restores_everything(build):
    off = open_sample(sample("flow", *build), d810=False)
    bv = open_sample(sample("flow", *build), d810=True)
    try:
        settle(bv)
        record, texts = apply.record_of(bv).to_json(), _texts(bv)
        for name in FIXTURE:
            function_named(bv, name).reanalyze()
        settle(bv)
        assert apply.record_of(bv).to_json() == record, "a second pass changed the record"
        assert _texts(bv) == texts, "a second pass changed the HLIL"
        _disable_all(bv)
        assert _texts(bv) == _texts(off)
        assert _no_record(bv), apply.record_of(bv).to_json()
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, bv)
        for func in list(bv.functions):
            func.reanalyze()
        settle(bv)
        assert _texts(bv) == texts, "re-enabling D-810 did not recover the same result"
    finally:
        bv.file.close()
        off.file.close()


def test_a_table_two_functions_claim_stays_until_both_release():
    bv = _fresh()
    try:
        change = _table_of(bv, "tbl_call_const")
        shared = function_named(bv, "tbl_call_shared")
        assert shared.start in change.claims
        _disable(bv, function_named(bv, "tbl_call_const"))
        assert apply.record_of(bv).get("table", change.key) is not None
        assert "f_b(" in text(function_named(bv, "tbl_call_shared"))
        _disable(bv, function_named(bv, "tbl_call_shared"))
        assert apply.record_of(bv).get("table", change.key) is None
        assert decoded(text(function_named(bv, "tbl_call_shared")))
    finally:
        bv.file.close()


def test_a_type_the_user_set_before_is_kept():
    bv = open_sample(sample("flow", *BUILD), d810=False)
    try:
        func = function_named(bv, "tbl_call_user")
        table = data_pointers(bv, func)[0]
        user = bv.parse_type_string("int64_t user_table[2]")[0]
        bv.define_user_data_var(table, user)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        assert decoded(text(function_named(bv, "tbl_call_user")))
        assert str(bv.get_data_var_at(table).type) == str(user)
        assert any("has a user type" in n for n in apply.notes_for(bv, func.start)), apply.notes_for(bv, func.start)
    finally:
        bv.file.close()


def test_a_type_the_user_set_after_d810_is_kept():
    bv = _fresh()
    try:
        change = _table_of(bv, "tbl_call_const")
        user = bv.parse_type_string("int64_t user_table[2]")[0]
        bv.define_user_data_var(change.key, user)
        _disable(bv, function_named(bv, "tbl_call_const"))
        _disable(bv, function_named(bv, "tbl_call_shared"))
        assert apply.record_of(bv).get("table", change.key) is None
        assert str(bv.get_data_var_at(change.key).type) == str(user)
    finally:
        bv.file.close()


def test_the_record_survives_a_saved_database(tmp_path):
    path = tmp_path / "flow.bndb"
    bv = _fresh()
    try:
        record = apply.record_of(bv).to_json()
        assert len(apply.record_of(bv)) and bv.create_database(str(path))
    finally:
        bv.file.close()
    bv = binaryninja.load(str(path))
    try:
        settle(bv)
        assert apply.record_of(bv).to_json() == record
        _disable_all(bv)
        assert _no_record(bv), apply.record_of(bv).to_json()
        assert decoded(text(function_named(bv, "tbl_call_const")))
    finally:
        bv.file.close()


def test_turning_the_flow_fixes_off_leaves_the_mba_pass_alone():
    mba_only = open_sample(sample("flow", *BUILD), d810=True, options={settings.FLOW_FIXES: False})
    bv = _fresh()
    try:
        settle(mba_only)
        assert _no_record(mba_only)
        Settings().set_bool(settings.FLOW_FIXES, False, bv)
        for func in list(bv.functions):
            func.reanalyze()
        settle(bv)
        assert _texts(bv) == _texts(mba_only)
        assert _no_record(bv), apply.record_of(bv).to_json()
    finally:
        bv.file.close()
        mba_only.file.close()


def test_undo_preserves_a_user_signature_and_inline_flag():
    bv = open_sample(sample("flow", "x86_64-pe", "O2"), d810=True)
    try:
        settle(bv)
        func = function_named(bv, "tbl_jump_const")
        func.set_user_type("int64_t target(int32_t user_value)")
        ident = function_named(bv, "ident")
        ident.set_user_inline_during_analysis(True)
        _disable_all(bv)
        assert func.has_user_type and "user_value" in str(func.type)
        assert int(ident.inline_during_analysis.value) == 1
        assert _no_record(bv)
    finally:
        bv.file.close()


def test_a_read_only_range_folds_its_data_and_clearing_it_restores():
    off = open_sample(sample("flow", *BUILD), d810=False)
    try:
        flag = data_pointers(off, function_named(off, "config_test"))[0]
        before = text(function_named(off, "config_test"))
    finally:
        off.file.close()
    bv = _fresh()
    try:
        Settings().set_string_list(settings.READ_ONLY_RANGES, [f"{flag:#x}-{flag + 3:#x}", "not a range"], bv)
        function_named(bv, "config_test").reanalyze()
        settle(bv)
        hlil = text(function_named(bv, "config_test"))
        assert "== 7" not in hlil and "!= 7" not in hlil, hlil
        assert apply.record_of(bv).get("range", flag) is not None
        Settings().set_string_list(settings.READ_ONLY_RANGES, [], bv)
        function_named(bv, "config_test").reanalyze()
        settle(bv)
        assert text(function_named(bv, "config_test")) == before
        assert apply.record_of(bv).get("range", flag) is None
    finally:
        bv.file.close()


@pytest.mark.parametrize("reopen", [False, True])
def test_ranges_follow_the_last_enabled_function(tmp_path, reopen):
    bv = open_sample(sample("flow", *BUILD), d810=False)
    try:
        func = function_named(bv, "config_test")
        before = text(func)
        flag = data_pointers(bv, func)[0]
        Settings().set_string_list(settings.READ_ONLY_RANGES, [f"{flag:#x}-{flag + 3:#x}"], bv)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        assert "== 7" not in text(func) and "!= 7" not in text(func)
        # An unrelated disabled function must not remove the range.
        function_named(bv, "f_a").reanalyze()
        settle(bv)
        assert apply.record_of(bv).get("range", flag) is not None
        if reopen:
            path = tmp_path / "ranges.bndb"
            assert bv.create_database(str(path))
            bv.file.close()
            bv = binaryninja.load(str(path))
        _disable(bv, function_named(bv, "config_test"))
        assert text(function_named(bv, "config_test")) == before
        assert _no_record(bv), apply.record_of(bv).to_json()
    finally:
        bv.file.close()


def test_a_range_keeps_a_table_after_its_functions_release():
    bv = _fresh()
    try:
        change = _table_of(bv, "tbl_call_const")
        Settings().set_string_list(settings.READ_ONLY_RANGES,
                                   [f"{change.key:#x}-{change.detail['end'] - 1:#x}"], bv)
        function_named(bv, "config_test").reanalyze()
        settle(bv)
        _disable(bv, function_named(bv, "tbl_call_const"))
        _disable(bv, function_named(bv, "tbl_call_shared"))
        assert "f_b(" in text(function_named(bv, "tbl_call_const"))
        Settings().set_string_list(settings.READ_ONLY_RANGES, [], bv)
        function_named(bv, "config_test").reanalyze()
        settle(bv)
        assert decoded(text(function_named(bv, "tbl_call_const")))
        assert apply.record_of(bv).get("table", change.key) is None
    finally:
        bv.file.close()


def test_a_read_only_range_preserves_user_types():
    bv = open_sample(sample("flow", *BUILD), d810=False)
    try:
        func = function_named(bv, "config_test")
        flag = data_pointers(bv, func)[0]
        user = bv.parse_type_string("uint32_t user_flag")[0]
        bv.define_user_data_var(flag, user)
        Settings().set_string_list(settings.READ_ONLY_RANGES, [f"{flag:#x}-{flag + 3:#x}"], bv)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        assert str(bv.get_data_var_at(flag).type) == str(user)
        assert "== 7" in text(func) or "!= 7" in text(func)
        _disable(bv, func)
        assert str(bv.get_data_var_at(flag).type) == str(user)
        assert _no_record(bv)
    finally:
        bv.file.close()


def test_flow_types_are_changed_outside_the_core_completion_callback(monkeypatch):
    import threading

    from binja_d810.binja.flow import evidence

    callback = threading.local()
    in_callback = []
    completed = apply._completed
    define = evidence.define_type

    def completion(view, state):
        callback.active = True
        try:
            completed(view, state)
        finally:
            callback.active = False

    def capture(view, address, t):
        in_callback.append(getattr(callback, "active", False))
        define(view, address, t)

    monkeypatch.setattr(apply, "_completed", completion)
    monkeypatch.setattr(evidence, "define_type", capture)
    bv = _fresh()
    try:
        assert in_callback and not any(in_callback)
        assert "f_b(" in text(function_named(bv, "tbl_call_const"))
        _disable_all(bv)
        assert _no_record(bv)
    finally:
        bv.file.close()


def test_disabling_a_function_cancels_its_queued_work(monkeypatch):
    import threading

    waiting, resume = threading.Event(), threading.Event()
    decide = apply.decide

    def pause(work, site):
        if not waiting.is_set():
            waiting.set()
            assert resume.wait(10), "the test did not release the queued round"
        decide(work, site)

    monkeypatch.setattr(apply, "decide", pause)
    bv = open_sample(sample("flow", *BUILD), d810=True)
    try:
        assert waiting.wait(10), "no flow round was queued"
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
        for func in list(bv.functions):
            func.reanalyze()
        bv.update_analysis_and_wait()
        resume.set()
        settle(bv)
        assert _no_record(bv), apply.record_of(bv).to_json()
        assert decoded(text(function_named(bv, "tbl_call_const")))
    finally:
        resume.set()
        bv.file.close()
