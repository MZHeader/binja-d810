"""Prove the bytes of a key independently of writes to its containing object."""

import pytest
from arch import IDS, MATRIX, sample
from helpers import function_named, global_reads, open_sample, settle
from flowcases import text

from binja_d810.binja.flow import apply, collect, evidence
from binja_d810.deadline import Deadline
from binja_d810.flow.record import Record

SAFE = ("bounded_loop", "bounded_mask", "bounded_boolean_loop", "readonly_call", "readonly_forward")
UNSAFE = ("overlap_last", "overlap_width", "unbounded_index", "unknown_initial", "written_callee",
          "saved_callee", "returned_callee", "mixed_callee", "indirect_callee", "signed_index", "reverse_index")


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    bv = open_sample(sample("key_bounds", *request.param), d810=False)
    yield bv
    bv.file.close()


def _span(bv, name):
    sites = list(collect.keys(function_named(bv, name).mlil))
    assert sites, name
    site = next(s for s in sites if bv.read_int(s.data, 4, False) == 0x537ac921)
    work = apply.Round(bv, apply.State(Record()))
    first = evidence.object_start(bv, site.data, uses_of=work.uses_of)
    return min(first, site.data - 32), site.data, site.data + 4


@pytest.mark.parametrize("name", SAFE + UNSAFE)
def test_only_proven_disjoint_writes_and_readonly_arguments_pass(builds, name):
    first, start, end = _span(builds, name)
    work = apply.Round(builds, apply.State(Record()))
    why = evidence.never_written(builds, start, end, work.uses_of, first=first)
    assert (why is None) == (name in SAFE), (name, why)
    if name in SAFE:
        # The buffer itself is mutable, except in the read-only call fixtures.
        whole = evidence.never_written(builds, first, end, work.uses_of)
        assert (whole is None) == name.startswith("readonly"), (name, whole)


@pytest.mark.parametrize("name", SAFE)
def test_an_expired_proof_does_not_accept_the_key(builds, name):
    first, start, end = _span(builds, name)
    work = apply.Round(builds, apply.State(Record()))
    assert evidence.never_written(builds, start, end, work.uses_of, Deadline.after(0), first=first)


def test_rejection_cache_distinguishes_bytes_inside_one_object(builds):
    from binja_d810.flow.tables import Load, Span

    first, start, end = _span(builds, "bounded_mask")
    work = apply.Round(builds, apply.State(Record()))
    load = Load("key", (start,), 4, first)
    assert apply._unwritable(work, load, Span(first, 1, end - first)) is not None
    assert apply._unwritable(work, load, Span(start, 4, 1)) is None


@pytest.mark.parametrize("name", ("readonly_call", "readonly_forward"))
def test_a_call_type_override_prevents_argument_binding(builds, name, monkeypatch):
    from binaryninja import Function, Type

    first, start, end = _span(builds, name)
    monkeypatch.setattr(Function, "get_call_type_adjustment", lambda *_: Type.int(4))
    work = apply.Round(builds, apply.State(Record()))
    assert evidence.never_written(builds, start, end, work.uses_of, first=first)


def test_a_missing_callee_body_does_not_count_as_readonly(builds):
    first, start, end = _span(builds, "readonly_forward")
    work = apply.Round(builds, apply.State(Record()))
    reader = function_named(builds, "readonly_forward")
    uses_of = lambda f: work.uses_of(f) if f == reader else None
    assert evidence.never_written(builds, start, end, uses_of, first=first)


def test_proof_work_is_bounded(builds):
    from binja_d810.binja.flow.access import Proof
    from binja_d810.flow.uses import READ, classify

    first, start, end = _span(builds, "readonly_forward")
    work = apply.Round(builds, apply.State(Record()))
    uses = work.uses_of(function_named(builds, "readonly_forward"))
    graph = Proof(work.uses_of, Deadline.never(), limit=0).graph(uses, (start, end))
    assert classify(uses.constants_in(first, end), graph)[0] != READ


def test_a_pointer_above_a_byte_does_not_hide_a_reverse_indexed_write(builds):
    _, key, _ = _span(builds, "reverse_index")
    byte = key - 32
    work = apply.Round(builds, apply.State(Record()))
    assert evidence.never_written(builds, byte, byte + 1, work.uses_of)


def test_modular_bounds_include_every_value_at_wrap_and_width_boundaries():
    from types import SimpleNamespace as Node
    from binaryninja import MediumLevelILOperation as Op
    from binja_d810.binja.flow.bounds import Bounds

    ranges = ((0, 255), (0, 7), (1, 28), (128, 135), (250, 255), (255, 255))
    operations = ((Op.MLIL_ADD, lambda a, b: a + b), (Op.MLIL_SUB, lambda a, b: a - b),
                  (Op.MLIL_MUL, lambda a, b: a * b), (Op.MLIL_AND, lambda a, b: a & b))
    left = Node(operation=Op.MLIL_VAR_SSA, size=1, src="left", il_basic_block=None)
    right = Node(operation=Op.MLIL_VAR_SSA, size=1, src="right", il_basic_block=None)
    for op, evaluate in operations:
        for a in ranges:
            for b in ranges:
                bounds = Bounds(None, Deadline.never())
                bounds.variable = lambda var, *_, a=a, b=b: a if var == "left" else b
                lo, hi = bounds.value(Node(operation=op, size=1, left=left, right=right))
                assert all(lo <= (evaluate(x, y) & 255) <= hi
                           for x in range(a[0], a[1] + 1) for y in range(b[0], b[1] + 1)), (op, a, b, lo, hi)


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_workflow_folds_safe_keys_and_restores_them(build):
    from binaryninja import Settings
    from binja_d810.binja import workflow

    bv = open_sample(sample("key_bounds", *build), d810=False)
    try:
        before = {name: text(function_named(bv, name)) for name in SAFE + UNSAFE}
        reads = {name: global_reads(function_named(bv, name)) for name in UNSAFE}
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        for name in SAFE:
            assert "0x11111111" in text(function_named(bv, name)), (name, text(function_named(bv, name)))
        for name in UNSAFE:
            assert global_reads(function_named(bv, name)) == reads[name], name
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        for name in SAFE + UNSAFE:
            assert text(function_named(bv, name)) == before[name], name
    finally:
        bv.file.close()
