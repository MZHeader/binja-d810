import pytest

from binja_d810.ir.expr import Const
from binja_d810.ir.parse import parse
from binja_d810.rules.registry import load_rules

RULES = {r.name: r for r in load_rules()}
PREDICATES = ["PredSetnzRule1", "PredSetzRule1", "PredSetbRule1", "PredOdd1", "PredOdd2", "PredFFRule1"]


@pytest.mark.parametrize("name", PREDICATES)
def test_predicate_rules_are_enabled(name):
    assert name in RULES, f"{name} not enabled: see verified_rules.json notes"


def test_setnz_folds_an_impossible_equality_as_a_value_and_as_a_condition():
    rule = RULES["PredSetnzRule1"]
    as_value = rule.match(parse("zx32(ne(x | 16, 15))", 32))
    assert as_value is not None and as_value.replacement == Const(1, 32)
    as_condition = rule.match(parse("ne(x | 16, 15)", 1, {"x": 32}))
    assert as_condition is not None and as_condition.replacement == Const(1, 1)
    assert rule.match(parse("ne(x | 1, 15)", 1, {"x": 32})) is None  # 1 | 15 == 15: possible


def test_odd_products_are_even():
    assert RULES["PredOdd1"].match(parse("(x * (x - 1)) & 1", 32)).replacement == Const(0, 32)


def test_setb_is_unsigned():
    assert RULES["PredSetbRule1"].match(parse("ult(x & 7, 8)", 1, {"x": 32})).replacement == Const(1, 1)
    assert RULES["PredSetbRule1"].match(parse("slt(x & 7, 8)", 1, {"x": 32})) is None


def test_constant_replacements_instantiate_as_constants():
    # The corpus generator builds expected answers from replacements (tests/samples/gen_mba.py).
    from binja_d810.rules.instantiate import instantiate

    assert instantiate(RULES["PredOdd1"].replacement, 32) == Const(0, 32)
    assert instantiate(RULES["PredFFRule1"].replacement, 64) == Const(2**64 - 1, 64)
