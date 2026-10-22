"""Returned and locally stored pointer values need a proof of all later uses."""

import pytest
from arch import IDS, MATRIX, sample
from helpers import data_pointers, function_named, open_sample, settle
from flowcases import text
from binja_d810.binja.flow import apply, evidence
from binja_d810.flow.record import Record

SAFE = ("return_read", "return_chain", "return_offset", "stored_local_read", "stored_local_return")
UNSAFE = ("return_write", "return_escape", "return_top_reader", "return_mixed", "return_indirect",
          "stored_local_write", "stored_local_escape", "stored_local_forward", "stored_partial", "stored_partial_write")


def key_of(bv, name):
    return next(a for a in data_pointers(bv, function_named(bv, name))
                if bv.read_int(a, 4, False) == 0x537ac921)


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    bv = open_sample(sample("pointer_uses", *request.param), d810=False)
    yield bv
    bv.file.close()


@pytest.mark.parametrize("name", SAFE + UNSAFE)
def test_proof_follows_returned_pointer_uses(builds, name):
    key = key_of(builds, name)
    work = apply.Round(builds, apply.State(Record()))
    first = evidence.object_start(builds, key, uses_of=work.uses_of)
    why = evidence.never_written(builds, key, key + 4, work.uses_of, first=first)
    assert (why is None) == (name in SAFE), (name, why)


def test_proof_graph_releases_its_native_snapshot_without_cyclic_gc(builds):
    import gc
    import weakref
    from binja_d810.binja.flow.access import Proof
    from binja_d810.deadline import Deadline

    work = apply.Round(builds, apply.State(Record()))
    graph = Proof(work.uses_of, Deadline.never()).graph(work.uses_of(function_named(builds, "stored_local_read")))
    ref = weakref.ref(graph)
    enabled = gc.isenabled()
    gc.disable()
    try:
        del graph
        assert ref() is None, "a proof must not retain native analysis snapshots in a reference cycle"
    finally:
        if enabled:
            gc.enable()


def test_summary_cache_distinguishes_a_pointer_from_its_slot_address(builds):
    from binaryninja import MediumLevelILOperation as Op
    from binja_d810.binja.flow.access import Proof
    from binja_d810.deadline import Deadline

    func = function_named(builds, "local_return")
    call = next(i for i in func.mlil.ssa_form.instructions if i.operation == Op.MLIL_CALL_SSA)
    work = apply.Round(builds, apply.State(Record()))
    proof = Proof(work.uses_of, Deadline.never())
    direct = proof.argument(call, call.params[0].expr_index, 0)
    indirect = proof.argument(call, call.params[0].expr_index, 0, indirect=True)
    assert direct is not None and not direct.returned
    assert indirect is not None and indirect.returned


@pytest.mark.parametrize("caller, callee, accepted", (
    ("stored_local_read", "local_read", True), ("stored_local_return", "local_return", True),
    ("stored_local_write", "local_write", False), ("stored_local_escape", "local_escape", False),
    ("stored_local_forward", "local_forward", False), ("stored_partial", "local_partial", False),
    ("stored_partial_write", "local_partial_write", False),
))
def test_slot_summary_checks_the_consumer_without_neighboring_objects(builds, caller, callee, accepted):
    from binaryninja import MediumLevelILOperation as Op
    from binja_d810.binja.flow.access import Proof
    from binja_d810.deadline import Deadline

    target = function_named(builds, callee).start
    call = next(i for i in function_named(builds, caller).mlil.ssa_form.instructions
                if i.operation == Op.MLIL_CALL_SSA and i.dest.constant == target)
    work = apply.Round(builds, apply.State(Record()))
    summary = Proof(work.uses_of, Deadline.never()).argument(call, call.params[0].expr_index, 0)
    assert (summary is not None) == accepted


@pytest.mark.parametrize("case", ("unknown", "wrong_entry", "displaced_entry", "frame"))
def test_propagated_returns_need_matching_native_values(case, monkeypatch):
    from types import SimpleNamespace
    from binaryninja import Function, RegisterValueType

    bv = open_sample("pointer_uses_O0.dylib", d810=False)
    try:
        helper = function_named(bv, "forward_pointer")
        native = Function.get_reg_value_after
        def value(func, address, reg):
            got = native(func, address, reg)
            if func != helper:
                return got
            assert got.type == RegisterValueType.EntryValue
            if case == "unknown":
                return SimpleNamespace(type=RegisterValueType.UndeterminedValue)
            if case == "frame":
                return SimpleNamespace(type=RegisterValueType.StackFrameOffset, value=0, offset=0)
            return SimpleNamespace(type=got.type, value=got.value + (case == "wrong_entry"),
                                   offset=8 if case == "displaced_entry" else 0)
        monkeypatch.setattr(Function, "get_reg_value_after", value)
        key = key_of(bv, "return_chain")
        work = apply.Round(bv, apply.State(Record()))
        assert evidence.never_written(bv, key, key + 4, work.uses_of,
                                       first=evidence.object_start(bv, key, uses_of=work.uses_of))
    finally:
        bv.file.close()


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_workflow_folds_safe_keys_and_undo_restores_them(build):
    from binaryninja import Settings
    from binja_d810.binja import workflow

    bv = open_sample(sample("pointer_uses", *build), d810=False)
    try:
        keys = {name: key_of(bv, name) for name in SAFE + UNSAFE}
        before = {name: text(function_named(bv, name)) for name in SAFE + UNSAFE}
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, bv)
        for f in bv.functions: f.reanalyze()
        settle(bv)
        for name, key in keys.items():
            change = apply.record_of(bv).get("table", key)
            assert (change is not None) == (name in SAFE), (name, apply.notes_for(bv, function_named(bv, name).start))
            if name in SAFE:
                assert "0x11111111" in text(function_named(bv, name))
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
        for f in bv.functions: f.reanalyze()
        settle(bv)
        for name in before:
            assert text(function_named(bv, name)) == before[name], name
    finally:
        bv.file.close()
