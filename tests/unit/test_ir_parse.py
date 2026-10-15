import random

import pytest
from exprgen import random_expr

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import BinOp, Const, UnOp, Var, mask, to_str
from binja_d810.ir.parse import ParseError, parse

a, b, c = (Var(n, 32) for n in "abc")


def test_c_precedence():
    assert parse("c ^ a & b", 32) == BinOp("xor", c, BinOp("and", a, b, 32), 32)
    assert parse("a + b * c", 32) == BinOp("add", a, BinOp("mul", b, c, 32), 32)
    assert parse("a | b ^ c", 32) == BinOp("or", a, BinOp("xor", b, c, 32), 32)


def test_literals_and_negative_folding():
    assert parse("0x10", 32) == Const(16, 32)
    assert parse("-1", 8) == Const(0xFF, 8)
    assert parse("a - -5", 32) == BinOp("sub", a, Const(-5, 32), 32)


def test_shift_is_logical_and_calls():
    assert parse("a >> 3", 32) == BinOp("lshr", a, Const(3, 32), 32)
    assert parse("sar(a, 3)", 32) == BinOp("ashr", a, Const(3, 32), 32)
    assert parse("zx64(p)", 64, {"p": 32}) == UnOp("zx", Var("p", 32), 64)


def test_cobra_style_output_evaluates():
    e = parse("22 * (x & y) + -17 * x + -5 * y", 32)
    x, y = 3, 5
    assert evaluate(e, {"x": x, "y": y}) == (22 * (x & y) - 17 * x - 5 * y) & mask(32)


def test_identifiers_with_ssa_suffixes():
    assert parse("x0#2 ^ opaque@17", 32) == BinOp("xor", Var("x0#2", 32), Var("opaque@17", 32), 32)


@pytest.mark.parametrize("bad", ["(a + b", "a +", "frob(a)", "zx32(a)", "a $ b"])
def test_errors(bad):
    with pytest.raises(ParseError):
        parse(bad, 32)


def test_round_trips_to_str():
    rng = random.Random(5)
    for _ in range(300):
        e = random_expr(rng)
        assert parse(to_str(e), 32) == e
