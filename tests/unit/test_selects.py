"""A condition that depends on its inputs only through one boolean (v4.2): the boolean, its inverse, or a
constant. Branchless selects (x86 neg; sbb; and; add) compared with one of their arms are the common case."""

from binja_d810.ir.expr import BinOp, Const, UnOp, Var
from binja_d810.semantic.selects import booleans, fold_select, replace
from binja_d810.semantic.verify import Verdict
from binja_d810.simplify import simplify_expression

a, b = Var("a", 32), Var("b", 32)
ne = BinOp("ne", a, Const(0, 32), 1)


def add(x, k):
    return BinOp("add", x, k if not isinstance(k, int) else Const(k, 32), 32)


def and_(x, k):
    return BinOp("and", x, Const(k, 32), 32)


def sub(x, y):
    return BinOp("sub", x, y, 32)


def neg(x):
    return UnOp("neg", x, 32)


def zx32(c):
    return UnOp("zx", c, 32)


SEL = add(and_(sub(sub(neg(a), neg(a)), zx32(ne)), 0x10), 0x20)  # a != 0 ? 0x30 : 0x20


def test_booleans_are_the_maximal_conditions_in_order():
    ult = BinOp("ult", a, b, 1)
    assert booleans(add(zx32(ult), zx32(BinOp("eq", zx32(ne), Const(1, 32), 1)))) == [
        ult, BinOp("eq", zx32(ne), Const(1, 32), 1)]


def test_replace_substitutes_every_occurrence():
    assert replace(add(zx32(ne), zx32(ne)), ne, Const(1, 1)) == add(zx32(Const(1, 1)), zx32(Const(1, 1)))


def test_select_equal_to_its_true_arm_is_the_condition():
    assert fold_select(BinOp("eq", SEL, Const(0x30, 32), 1)) == ne


def test_select_equal_to_its_false_arm_is_the_inverse():
    assert fold_select(BinOp("eq", SEL, Const(0x20, 32), 1)) == BinOp("eq", a, Const(0, 32), 1)


def test_select_equal_to_neither_arm_is_false():
    assert fold_select(BinOp("eq", SEL, Const(0x99, 32), 1)) == Const(0, 1)


def test_inverse_of_an_ordered_comparison():
    lt = BinOp("ult", a, b, 1)
    assert fold_select(BinOp("eq", zx32(lt), Const(0, 32), 1)) == BinOp("uge", a, b, 1)


def test_inverse_of_a_flag_variable():
    flag = Var("c", 1)
    folded = fold_select(BinOp("eq", zx32(flag), Const(0, 32), 1))
    assert folded == BinOp("eq", flag, Const(0, 1), 1)


def test_two_booleans_are_not_folded():
    assert fold_select(BinOp("eq", add(zx32(ne), zx32(BinOp("ult", a, b, 1))), Const(1, 32), 1)) is None


def test_a_variable_outside_the_boolean_is_not_folded():
    assert fold_select(BinOp("eq", add(zx32(ne), b), Const(1, 32), 1)) is None


def test_a_wide_expression_is_not_folded():
    assert fold_select(SEL) is None


def test_simplify_folds_the_select_compare_with_a_proof():
    out = simplify_expression(BinOp("eq", SEL, Const(0x30, 32), 1), rules=[], backends=[])
    assert out.rewrite is not None, out
    assert out.rewrite.replacement == ne
    assert out.rewrite.verdict is Verdict.PROVEN and "select" in out.rewrite.source
