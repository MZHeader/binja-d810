import pytest

from binja_d810.deadline import Deadline
from binja_d810.flags.recombine import Limits, recombine
from binja_d810.flags.semantics import CARRY_IS_NO_BORROW, add_overflow, choose, condition, subtraction
from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import BinOp, Const, Var, CMP_OPS, to_signed
from binja_d810.ir.normalize import normalize
from binja_d810.semantic.verify import Verdict, check_equivalent


@pytest.mark.parametrize("architecture", CARRY_IS_NO_BORROW)
@pytest.mark.parametrize("width", (4, 8, 16, 32, 64))
@pytest.mark.parametrize("comparison", sorted(CMP_OPS))
def test_all_flag_conditions_have_exact_width_proofs(architecture, width, comparison):
    a, b = Var("a", width), Var("b", width)
    flags = subtraction(a, b, architecture)
    original = condition(flags, comparison, architecture)
    expected = BinOp(comparison, a, b, 1)
    assert check_equivalent(original, expected) is Verdict.PROVEN
    found = recombine(original).expression
    assert check_equivalent(found, expected) is Verdict.PROVEN
    assert normalize(found) == normalize(expected)


def test_overflow_matches_independent_exhaustive_signed_arithmetic():
    a, b = Var("a", 4), Var("b", 4)
    addition = add_overflow(a, b)
    overflow = subtraction(a, b, "aarch64")["v"]
    for x in range(16):
        for y in range(16):
            signed_x, signed_y = to_signed(x, 4), to_signed(y, 4)
            assert evaluate(addition, {"a": x, "b": y}) == (not -8 <= signed_x + signed_y <= 7)
            assert evaluate(overflow, {"a": x, "b": y}) == (not -8 <= signed_x - signed_y <= 7)


@pytest.mark.parametrize("otherwise", (0, 1))
def test_conditional_flags_recombine_through_their_selector(otherwise):
    a, b, g = Var("a", 32), Var("b", 32), Var("g", 1)
    values = subtraction(a, b, "aarch64")
    selected = {k: choose(g, value, Const(otherwise if k == "n" else 0, 1))
                for k, value in values.items()}
    original = condition(selected, "slt", "aarch64")
    expected = choose(g, BinOp("slt", a, b, 1), Const(otherwise, 1))
    result = recombine(original, guards=(g,))
    assert result.expression == expected
    assert check_equivalent(original, result.expression) is Verdict.PROVEN


def test_flag_reconstruction_does_not_guess_overflow_or_borrow():
    a, b = Var("a", 32), Var("b", 32)
    n = subtraction(a, b, "aarch64")["n"]
    assert check_equivalent(n, BinOp("slt", a, b, 1)) is Verdict.REFUTED
    assert recombine(n).expression != BinOp("slt", a, b, 1)


def test_every_flag_reconstruction_budget_fails_closed():
    a, b = Var("a", 32), Var("b", 32)
    original = condition(subtraction(a, b, "aarch64"), "sle", "aarch64")
    for limits, deadline in ((Limits(nodes=1), Deadline.never()),
                             (Limits(work=0), Deadline.never()),
                             (Limits(), Deadline.after(0))):
        result = recombine(original, limits=limits, deadline=deadline)
        assert result.expression == original and result.notes
