"""v4.0: jumps and calls through tables of code pointers, identity wrappers and read-only data
(tests/samples/src/flow.c), on 8 builds."""

import pytest
from arch import IDS, MATRIX, sample
from flowcases import CALL_TARGETS, CALLS, JUMP_LABELS, JUMPS, NOT_EMITTED, RAW, check, decoded, text
from helpers import function_named, open_sample

from binja_d810.binja.flow import collect, evidence
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
