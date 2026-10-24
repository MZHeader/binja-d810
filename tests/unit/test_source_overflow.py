import pytest
from binja_d810.flags.semantics import add_overflow, subtract_overflow_bits, subtract_overflow_wide
from binja_d810.ir.expr import Var, UnOp
from binja_d810.ir.evaluate import evaluate
from binja_d810.semantic.verify import Verdict, check_equivalent


@pytest.mark.parametrize('width', (8, 16, 32, 64))
def test_subtraction_overflow_is_proven_against_a_widened_signed_difference(width):
    a, b = Var('a', width), Var('b', width)
    assert check_equivalent(subtract_overflow_bits(a,b), subtract_overflow_wide(a,b)) is Verdict.PROVEN


@pytest.mark.parametrize('width', (8, 16, 32, 64))
def test_negated_minimum_does_not_turn_subtraction_into_ordinary_addition_overflow(width):
    a, b = Var('a', width), Var('b', width)
    values = {'a': 0, 'b': 1 << (width - 1)}
    assert evaluate(subtract_overflow_bits(a,b), values) == 1
    assert evaluate(add_overflow(a,UnOp('neg',b,width)), values) == 0
