"""v4.0: jumps and calls through tables of code pointers, identity wrappers and read-only data
(tests/samples/src/flow.c), on 8 builds."""

import pytest
from arch import IDS, MATRIX, sample
from flowcases import (CALL_TARGETS, CALLS, JUMP_LABELS, JUMPS, KNOWN_MISSES, NEGATIVES, NOT_EMITTED, RAW, check,
                       decoded, has, text)
from helpers import data_pointers, function_named, open_sample, settle

from binja_d810.binja.flow import apply, collect, evidence
from binja_d810.deadline import Deadline
from binja_d810.flow.codecheck import jump_problem
from binja_d810.flow.tables import decode


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def off(request):
    bv = open_sample(sample("flow", *request.param), d810=False)
    yield "-".join(request.param), bv
    bv.file.close()


@pytest.mark.parametrize("name", JUMPS)
def test_the_jump_is_unresolved_without_d810(off, name):
    key, bv = off
    hlil = text(function_named(bv, name))
    if (key, name) in NOT_EMITTED:
        assert "jump(" not in hlil, f"listed as not emitted, but the jump is there: {(key, name)}"
        return
    assert "jump(" in hlil, hlil


@pytest.mark.parametrize("name", CALLS)
def test_the_call_is_indirect_without_d810(off, name):
    key, bv = off
    hlil = text(function_named(bv, name))
    if (key, name) in NOT_EMITTED:
        assert not decoded(hlil), f"listed as not emitted, but the decode is there: {(key, name)}"
        return
    assert (decoded(hlil) or name in RAW) and "f_a(" not in hlil and "f_b(" not in hlil, hlil


def test_the_wrappers_are_called_without_d810(off):
    key, bv = off
    hlil = text(function_named(bv, "ident_call"))
    if (key, "ident_call") in NOT_EMITTED:
        assert "ident(" not in hlil, f"listed as not emitted, but the wrapper is called: {(key, 'ident_call')}"
    else:
        assert "ident(" in hlil, hlil
    assert "not_ident(" in text(function_named(bv, "not_identity_call"))


def test_the_flag_is_tested_without_d810(off):
    _, bv = off
    hlil = text(function_named(bv, "config_test"))
    assert "== 7" in hlil or "!= 7" in hlil, hlil


def _sites(bv, name, kinds=("jump", "call")):
    func = function_named(bv, name)
    return [site for site, _ in collect.scan(func.mlil, Deadline.after(5.0), evidence.reader(bv)) if site.kind in kinds]


def _decoded(bv, name):
    sites = _sites(bv, name)
    if len(sites) != 1:
        return None, f"{len(sites)} sites"
    return decode(sites[0].target.expr, sites[0].target.loads, evidence.reader(bv))


@pytest.mark.parametrize("name", sorted(JUMP_LABELS))
def test_a_jump_site_decodes_to_its_labels(off, name):
    key, bv = off
    found, why = _decoded(bv, name)
    func = function_named(bv, name)
    ok = (found is not None and len(set(found.targets)) == len(JUMP_LABELS[name])
          and jump_problem(found.targets, func.start, evidence.BinaryCode(bv, func.arch)) is None)
    check(key, name, ok, f"{why} {found}")


@pytest.mark.parametrize("name", sorted(CALL_TARGETS))
def test_a_call_site_decodes_to_its_function(off, name):
    key, bv = off
    found, why = _decoded(bv, name)
    names = [] if found is None else [
        f.name.lstrip("_") if (f := bv.get_function_at(t)) is not None else hex(t) for t in found.targets]
    check(key, name, names == [CALL_TARGETS[name]], f"{why} {names}")


def test_a_key_global_in_the_index_is_read_as_a_key(off):
    key, bv = off
    sites = _sites(bv, "tbl_jump_keyindex")
    loads = sites[0].target.loads if len(sites) == 1 else ()
    check(key, "tbl_jump_keyindex", len(loads) == 2 and all(len(load.addresses) == 1 for load in loads), str(loads))


def test_a_wrapper_call_is_collected(off):
    key, bv = off
    sites = _sites(bv, "ident_call", kinds=("wrapper",))
    check(key, "ident_call", [s.callee for s in sites] == [function_named(bv, "ident").start], str(sites))


def test_plain_code_has_no_site(off):
    _, bv = off
    for name in ("config_test", "f_a", "ident", "not_ident"):
        assert not _sites(bv, name, kinds=("jump", "call", "wrapper")), name


NATIVE_JUMPS = {k: v for k, v in JUMP_LABELS.items() if k != "mba_jump"}  # resolved once their tables are const
NATIVE_CALLS = {k: v for k, v in CALL_TARGETS.items() if k != "mba_call"}


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    """(build-level, D-810 on and settled, D-810 off)."""
    on = open_sample(sample("flow", *request.param), d810=True)
    settle(on)
    off = open_sample(sample("flow", *request.param), d810=False)
    yield "-".join(request.param), on, off
    on.file.close()
    off.file.close()


def _resolved_jump(hlil: str, labels: list[str]) -> bool:
    return "jump(" not in hlil and all(has(hlil, c) for c in labels)


def _direct_call(hlil: str, callee: str) -> bool:
    return f"{callee}(" in hlil and not decoded(hlil)


def _explain(bv, name: str) -> str:
    func = function_named(bv, name)
    return text(func) + "\n-- flow notes --\n" + "\n".join(apply.notes_for(bv, func.start))


@pytest.mark.parametrize("name", sorted(NATIVE_JUMPS))
def test_a_jump_through_a_never_written_table_is_resolved(builds, name):
    key, on, _ = builds
    check(key, name, _resolved_jump(text(function_named(on, name)), NATIVE_JUMPS[name]), _explain(on, name))


@pytest.mark.parametrize("name", sorted(NATIVE_CALLS))
def test_a_call_through_a_never_written_table_is_direct(builds, name):
    key, on, _ = builds
    check(key, name, _direct_call(text(function_named(on, name)), NATIVE_CALLS[name]), _explain(on, name))


@pytest.mark.parametrize("name", NEGATIVES)
def test_a_negative_is_left_alone(builds, name):
    key, on, off = builds
    if (key, name) in NOT_EMITTED:
        return
    assert text(function_named(on, name)) == text(function_named(off, name)), _explain(on, name)


def test_a_shared_table_is_claimed_by_both_readers(builds):
    key, on, off = builds
    names = ("tbl_call_const", "tbl_call_shared")
    if any((key, n) in NOT_EMITTED or (key, n) in KNOWN_MISSES for n in names):
        return
    table = data_pointers(off, function_named(off, "tbl_call_const"))[0]
    change = apply.record_of(on).overlapping(table, table + 16)
    assert change is not None and change.kind == "table", apply.record_of(on).to_json()
    assert {function_named(on, n).start for n in names} <= change.claims


def test_rounds_continue_past_the_cap(monkeypatch):
    """With one site per round, rounds keep coming while analysis is idle, until every positive resolves."""
    monkeypatch.setattr(apply, "SITES_PER_ROUND", 1)
    build = MATRIX[0]
    key = "-".join(build)
    bv = open_sample(sample("flow", *build), d810=True)
    try:
        settle(bv, seconds=300)
        for name, labels in NATIVE_JUMPS.items():
            check(key, name, _resolved_jump(text(function_named(bv, name)), labels), _explain(bv, name))
        for name, callee in NATIVE_CALLS.items():
            check(key, name, _direct_call(text(function_named(bv, name)), callee), _explain(bv, name))
    finally:
        bv.file.close()
