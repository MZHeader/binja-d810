"""v4.3: constant keys in writable data (tests/samples/src/keys.c), on 8 builds: a never-written key that encodes
constants is typed const and what it encodes folds; exported, written, escaped, relocated and compared-only
globals stay (a compare after an operation with a constant is still a compare); turning D-810 off restores the key's type."""

import pytest
from arch import IDS, MATRIX, expect_miss, sample
from binaryninja import Settings
from flowcases import text
from helpers import function_named, global_reads, open_sample, settle

from binja_d810.binja import workflow
from binja_d810.binja.flow import apply, collect, evidence
from binja_d810.flow.record import Record

# What each function must show once its key is constant, and what it must lose.
FOLDED = {"key_xor": (("0x11111111",), ()), "key_mba": (("0xa5e84e4b", "0x5a17b1b5"), ("|",)),
          "key_shift": (("<< 5", "<< 0x5a17c405"), ()), "key_mul64": (("* 3", "3 *", ", 3)", "(3, "), ("^",))}
# arm64 masks a shift amount itself, so clang drops the & 31: x << (K + 0x1c), shown folded as << 0x5a17c405.
STATES = ("0x7a3c19e5", "0x1b4d8c27", "0x5e91a3f0", "0x33c6d218", "0x202455fc", "0xc135c83e", "0x479e007",
          "0xd9af0e2f")  # key_states' raw and encoded states
NEGATIVES = ("exported_key", "written_key", "set_key", "escaped_key", "address_key", "level_test", "key_branch",
             "unset_key")


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    """(build-level, D-810 on and settled, D-810 off)."""
    on = open_sample(sample("keys", *request.param), d810=True)
    settle(on)
    off = open_sample(sample("keys", *request.param), d810=False)
    yield "-".join(request.param), on, off
    on.file.close()
    off.file.close()


def _keys(bv) -> list:
    return [c for c in apply.record_of(bv).changes() if c.kind == "table" and not c.detail["targets"]]


@pytest.mark.parametrize("name", sorted(FOLDED))
def test_a_constant_encoded_against_a_key_folds(request, builds, name):
    key, on, off = builds
    expect_miss(request, name, key)
    shown, lost = FOLDED[name]
    hlil = text(function_named(on, name))
    assert any(s in hlil for s in shown) and not any(s in hlil for s in lost), hlil
    assert not any(s in text(function_named(off, name)) for s in shown)


def test_a_machine_whose_states_are_encoded_against_a_key_is_unflattened(request, builds):
    key, on, _ = builds
    expect_miss(request, "key_states", key)
    hlil = text(function_named(on, "key_states"))
    assert not [s for s in STATES if s in hlil], hlil


@pytest.mark.parametrize("name", NEGATIVES)
def test_a_global_that_is_not_a_key_stays(builds, name):
    _, on, off = builds
    # Stack cleanup may remove unrelated initializers; every global read must remain.
    assert global_reads(function_named(on, name)) == global_reads(function_named(off, name))
    assert not any(c.key <= address < c.detail["end"]
                   for c in _keys(on) for address, _ in global_reads(function_named(off, name)))


def test_the_report_names_the_key(request, builds):
    key, on, _ = builds
    expect_miss(request, "key_report", key)
    readers = [function_named(on, name).start for name in [*FOLDED, "key_states"]]
    notes = [n for start in readers for n in apply.notes_for(on, start)]
    assert any("a key, read by" in n for n in notes), notes
    xor = function_named(on, "key_xor").start
    assert any(xor in c.detail["readers"] for c in _keys(on)), apply.record_of(on).to_json()


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_turning_d810_off_restores_the_key(build):
    off = open_sample(sample("keys", *build), d810=False)
    bv = open_sample(sample("keys", *build), d810=True)
    try:
        settle(bv)
        keys = _keys(bv)
        assert keys
        before = {c.key: str(off.get_data_var_at(c.key).type) for c in keys}
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
        for func in list(bv.functions):
            func.reanalyze()
        settle(bv)
        assert {k: str(bv.get_data_var_at(k).type) for k in before} == before
        assert len(apply.record_of(bv)) == 0, apply.record_of(bv).to_json()
        for name in [*FOLDED, "key_states"]:
            assert text(function_named(bv, name)) == text(function_named(off, name)), name
    finally:
        bv.file.close()
        off.file.close()


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_zero_keys_are_rejected_without_analyzing_their_readers(build):
    bv = open_sample(sample("keys", *build), d810=False)
    try:
        site = next(collect.keys(function_named(bv, "key_mul64").mlil))
        assert bv.write(site.data, bytes(collect.KEY_BYTES)) == collect.KEY_BYTES
        bv.update_analysis_and_wait()
        work = apply.Round(bv, apply.State(Record(), enabled={site.function}))
        assert work.read(site.data, collect.KEY_BYTES) == 0
        assert site.data not in {s.data for s in collect.keys(function_named(bv, "key_mul64").mlil)}

        def unexpected_reader(func):
            pytest.fail("a zero key must not request another function's IL")

        work.uses_of = unexpected_reader
        apply.decide(work, site)
        assert not work.touched and len(work.record) == 0
        assert any("skipped: zero" in line for line in work.state.notes[site.function])
    finally:
        bv.file.close()


def test_a_zero_byte_does_not_hide_a_wider_nonzero_key():
    from dataclasses import replace

    bv = open_sample("keys_O2.dylib", d810=False)
    try:
        site = next(collect.keys(function_named(bv, "key_mul64").mlil))
        assert bv.write(site.data, b"\x00") == 1  # only the low byte of this synthetic key is zero
        bv.update_analysis_and_wait()
        work = apply.Round(bv, apply.State(Record(), enabled={site.function}))
        assert work.read(site.data, 1) == 0 and work.read(site.data, collect.KEY_BYTES) != 0
        apply.decide(work, replace(site, size=1))
        typed = bv.get_data_var_at(site.data)
        assert typed is not None and typed.type.width == 8 and evidence.is_const(typed.type), work.state.notes
        assert work.record.get("table", site.data) is not None
    finally:
        bv.file.close()
