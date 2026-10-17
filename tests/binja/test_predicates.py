"""Opaque predicates and branch conditions (v3): folded only when proven; ordinary ones stay."""

import pytest
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, set_types

TYPES = {name: f"uint32_t {name}(uint32_t x, uint32_t y)" for name in
         ("opaque_parity", "opaque_setnz", "opaque_mba", "plain_condition")}


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def fixtures(request):
    bv = open_sample(sample("predicates", *request.param), d810=True)
    set_types(bv, TYPES)
    yield bv
    bv.file.close()


@pytest.mark.parametrize("name", ["opaque_parity", "opaque_setnz", "opaque_mba"])
def test_opaque_conditions_are_folded_and_the_dead_branch_is_gone(fixtures, name):
    func = function_named(fixtures, name)
    text = "\n".join(map(str, func.hlil.instructions))
    assert "if (" not in text and "0x5a" not in text, text  # the dead branch returns x ^ 0x5a


def test_ordinary_conditions_stay(fixtures):
    func = function_named(fixtures, "plain_condition")
    assert "if (" in "\n".join(map(str, func.hlil.instructions))
