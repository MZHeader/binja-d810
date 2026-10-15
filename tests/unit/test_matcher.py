from binja_d810.ir.expr import BinOp, Const, UnOp, Var
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.matching.matcher import match
from binja_d810.third_party.d810.mba.dsl import Const as PConst
from binja_d810.third_party.d810.mba.dsl import Var as PVar
from binja_d810.third_party.d810.mba.dsl import Zext

x, y = PVar("x_0"), PVar("x_1")
TWO, MINUS_ONE = PConst("2", 2), PConst("-1", -1)
a, b = Var("a", 32), Var("b", 32)


def ir(text):
    return normalize(parse(text, 32))


def test_binds_variables():
    assert {"x_0": a, "x_1": b} in list(match((x | y) - (x & y), ir("(a | b) - (a & b)")))


def test_commutative_operands():
    assert list(match((x | y) - (x & y), ir("(b | a) - (b & a)")))


def test_repeated_variable_must_bind_equal_subtrees():
    assert not list(match((x | y) - (x & y), ir("(a | b) - (a & c)")))


def test_concrete_constant_against_normalized_shift():
    assert list(match(TWO * (x & y), ir("(a & b) << 1")))
    assert list(match((x & y) << PConst("1", 1), ir("2 * (a & b)")))


def test_add_pattern_against_sub_target_binds_negated_constant():
    (bindings,) = list(match(x + PConst("c_1"), ir("a - 5")))
    assert bindings["c_1"] == Const(-5, 32)


def test_sub_pattern_against_add_target():
    (bindings,) = list(match(x - PConst("c_1"), BinOp("add", a, Const(7, 32), 32)))
    assert bindings["c_1"] == Const(-7, 32)


def test_patterns_written_in_unnormalized_forms():
    assert list(match(x ^ MINUS_ONE, ir("~a")))
    assert list(match(MINUS_ONE * x, ir("-a")))
    assert list(match(PConst("0", 0) - x, ir("-a")))


def test_bnot_of_symbolic_constant_matches_a_constant():
    (bindings,) = list(match(~PConst("c_1"), Const(0xFFFFFFF0, 32)))
    assert bindings["c_1"] == Const(0xF, 32)


def test_zext():
    p8 = Var("p", 8)
    target = UnOp("zx", BinOp("and", p8, Const(1, 8), 8), 32)
    (bindings,) = list(match(Zext(x & PConst("1", 1), 32), target))
    assert bindings["x_0"] == p8


def test_operation_mismatch():
    assert not list(match(x & y, ir("a | b")))
