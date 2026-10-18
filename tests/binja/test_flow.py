"""v4.0: jumps and calls through tables of code pointers, identity wrappers and read-only data
(tests/samples/src/flow.c), on 8 builds."""

import pytest
from arch import IDS, MATRIX, sample
from flowcases import CALLS, JUMPS, NOT_EMITTED, RAW, decoded, text
from helpers import function_named, open_sample


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
