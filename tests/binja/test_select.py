"""Branchless selects through the carry flag (v4.2): sbb and adc are lifted, and a condition that depends on
its inputs only through one boolean folds to that boolean (tests/samples/src/select_x86_64.S)."""

import pytest
from binaryninja import HighLevelILOperation as H
from helpers import function_named, lift_return, open_sample, set_types, strip_versions

from binja_d810.binja.hlil.lift import lift as lift_hlil
from binja_d810.ir.evaluate import evaluate

SIGNATURES = {
    "sel_sbb": "uint32_t sel_sbb(uint32_t x)",
    "sel_adc": "uint32_t sel_adc(uint32_t x, uint32_t y)",
    "sel_cmp": "uint32_t sel_cmp(uint32_t x)",
}


@pytest.fixture(scope="module")
def view():
    bv = open_sample("select_x86_64.so", d810=False)
    set_types(bv, SIGNATURES)
    yield bv
    bv.file.close()


def test_sbb_is_lifted(view):
    e = strip_versions(lift_return(function_named(view, "sel_sbb")))
    assert evaluate(e, {"x": 0}) == 0x20 and evaluate(e, {"x": 7}) == 0x30


def test_adc_is_lifted(view):
    e = strip_versions(lift_return(function_named(view, "sel_adc")))
    assert evaluate(e, {"x": 0, "y": 5}) == 5 and evaluate(e, {"x": 9, "y": 5}) == 6


def test_hlil_lifts_sbb(view):
    ssa = function_named(view, "sel_sbb").hlil.ssa_form
    (ret,) = [i for i in ssa.instructions if i.operation == H.HLIL_RET]
    e = lift_hlil(ret.src[0], check_versions=False)
    assert e is not None, str(ret)
    e = strip_versions(e)
    assert evaluate(e, {"x": 0}) == 0x20 and evaluate(e, {"x": 7}) == 0x30
