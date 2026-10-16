"""Comparison results used as values (setcc/cset) inside MBA expressions (v2.1)."""

import oracle
import pytest
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, set_types

TYPES = {name: f"uint32_t {name}(uint32_t x, uint32_t y)" for name in ("bool_ne", "bool_eq", "bool_wide")}
EXPECTED = {"bool_ne": "x + y", "bool_eq": "x + y", "bool_wide": "((x | ((x & 3) != 0)) ^ y) + 2 * ((x | ((x & 3) != 0)) & y)"}


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def fixtures(request):
    bv = open_sample(sample("hlil_fixtures", *request.param), d810=True)
    set_types(bv, TYPES)
    yield bv
    bv.file.close()


@pytest.mark.parametrize("name", ["bool_ne", "bool_eq"])
def test_a_zero_or_one_comparison_is_its_value(fixtures, name):
    func = function_named(fixtures, name)
    final = oracle.function_result(func)
    result = oracle.compare(final, oracle.reference(EXPECTED[name], 32), 32)
    assert result.kind == "proven", (result, "\n".join(map(str, func.hlil.instructions)))
    assert "==" not in str(func.hlil) and "!=" not in str(func.hlil), "\n".join(map(str, func.hlil.instructions))


def test_other_comparisons_keep_their_meaning(fixtures):
    func = function_named(fixtures, "bool_wide")
    final = oracle.function_result(func)
    result = oracle.compare(final, oracle.reference(EXPECTED["bool_wide"], 32), 32)
    assert result.kind == "proven", (result, "\n".join(map(str, func.hlil.instructions)))
