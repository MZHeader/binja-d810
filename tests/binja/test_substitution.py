"""Bitwise substitution and polynomial predicates (v4.3): collapsed and folded; ordinary bitwise code stays."""

import pytest
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, set_types

from binja_d810.binja import log, settings
from oracle import compare, function_result, reference

TYPES = {name: f"uint32_t {name}(uint32_t x, uint32_t y)" for name in (
    "sub_or", "sub_and", "sub_xor", "sub_bool", "sub_const", "xor_cancel", "plain_mask", "plain_or", "mix", "merge",
    "sq_pred", "sq_div", "sq_near")}
TYPES["sq_merged"] = "uint32_t sq_merged(uint32_t x, uint32_t y, uint32_t n)"
# What each substitution must lose: the operations (or constant) only its encoded form has.
COLLAPSED = {"sub_or": ("^", "&"), "sub_and": ("^", "~"), "sub_xor": ("~", "&"), "sub_bool": ("^",),
             "sub_const": ("0xf0",), "xor_cancel": ("^",)}
CONTROLS = ("plain_mask", "plain_or", "mix", "merge")
EXPECTED = {"plain_mask": "x & 255", "plain_or": "x | y",
            "mix": "((x >> 8) & 255) | ((x >> 16) & 65280)",
            "merge": "(x & 61680) | (y & ~61680)"}


def _open(build, level, d810):
    # Isolate substitution from scratch cleanup: the latter legitimately removes unused O0
    # parameter spills, including the uninitialized half of a combined x64 stack variable.
    bv = open_sample(sample("substitution", build, level), d810=d810, options={settings.SCRATCH: False})
    set_types(bv, TYPES)
    return bv


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def views(request):
    on, off = _open(*request.param, d810=True), _open(*request.param, d810=False)
    yield on, off
    on.file.close()
    off.file.close()


def _text(bv, name) -> str:
    return "\n".join(map(str, function_named(bv, name).hlil.instructions))


@pytest.mark.parametrize("name", COLLAPSED)
def test_substitutions_collapse(views, name):
    text = _text(views[0], name)
    assert not [token for token in COLLAPSED[name] if token in text], text
    for level, report in log.latest_reports(function_named(views[0], name)).items():
        assert not report.rolled_back and not report.errors, (level, report.errors)


@pytest.mark.parametrize("name", CONTROLS)
def test_ordinary_bitwise_code_is_unchanged(views, name):
    assert _text(views[0], name) == _text(views[1], name)
    func = function_named(views[0], name)
    assert not any(report.rewrites for report in log.latest_reports(func).values())
    result = compare(function_result(func), reference(EXPECTED[name], 32), 32)
    assert result.kind in ("proven", "exhaustive"), result


@pytest.mark.parametrize("name", ["sq_pred", "sq_div", "sq_merged"])
def test_polynomial_predicates_fold(views, name):
    text = _text(views[0], name)
    assert "0x5a" not in text, text  # the dead branch xors with 0x5a


def test_a_satisfiable_near_miss_stays(views):
    assert "if (" in _text(views[0], "sq_near")
