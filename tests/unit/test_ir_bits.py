import random

from exprgen import random_expr

from binja_d810.ir.bits import decided_comparison, possible_ones, upper_bound
from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import BinOp, Const, UnOp, Var, mask
from binja_d810.ir.parse import parse

x, y = Var("x", 32), Var("y", 32)


def test_a_masked_value_can_only_have_the_mask_bits():
    assert possible_ones(parse("x & 1", 32)) == 1
    assert possible_ones(parse("(x & 6) | (y & 1)", 32)) == 7
    assert possible_ones(parse("(x & 1) ^ 1", 32)) == 1


def test_unknown_values_may_have_every_bit():
    assert possible_ones(x) == mask(32)
    assert possible_ones(parse("(x & 1) + (y & 1)", 32)) == mask(32)  # a carry reaches bit 1
    assert possible_ones(parse("-(x & 1)", 32)) == mask(32)


def test_extensions_truncations_and_constant_shifts_move_the_bits():
    assert possible_ones(UnOp("zx", BinOp("and", Var("b", 8), Const(1, 8), 8), 32)) == 1
    assert possible_ones(UnOp("sx", BinOp("and", Var("b", 8), Const(1, 8), 8), 32)) == 1
    assert possible_ones(UnOp("sx", BinOp("and", Var("b", 8), Const(0x80, 8), 8), 32)) == 0xFFFFFF80
    assert possible_ones(UnOp("trunc", BinOp("and", x, Const(0x1FF, 32), 32), 8)) == 0xFF
    assert possible_ones(parse("(x & 0x10) >> 4", 32)) == 1
    assert possible_ones(parse("(x & 1) << 3", 32)) == 8


def test_possible_ones_is_sound_on_random_expressions():
    rng = random.Random(7)
    ops = ("add", "sub", "mul", "and", "or", "xor", "neg", "not", "shl")
    for _ in range(400):
        e = random_expr(rng, width=16, depth=4, ops=ops)
        bits = possible_ones(e)
        for _ in range(16):
            env = {name: rng.getrandbits(16) for name in ("a", "b", "c")}
            assert evaluate(e, env) & ~bits == 0


def test_a_comparison_with_zero_of_a_zero_or_one_value_is_that_value_or_its_complement():
    from binja_d810.ir.bits import compare_with_zero

    t = parse("x & 1", 32)
    assert compare_with_zero(t, "ne", 32) == t
    assert compare_with_zero(t, "eq", 32) == BinOp("xor", t, Const(1, 32), 32)
    assert compare_with_zero(t, "ne", 8) == UnOp("trunc", t, 8)
    assert compare_with_zero(UnOp("trunc", t, 8), "ne", 32) == UnOp("zx", UnOp("trunc", t, 8), 32)
    for value in (0, 1):
        env = {"x": value}
        assert evaluate(compare_with_zero(t, "ne", 32), env) == int(value != 0)
        assert evaluate(compare_with_zero(t, "eq", 32), env) == int(value == 0)


def test_other_comparisons_with_zero_are_not_modelled():
    from binja_d810.ir.bits import compare_with_zero

    assert compare_with_zero(parse("x & 3", 32), "ne", 32) is None
    assert compare_with_zero(x, "eq", 32) is None


def test_comparisons_decided_by_bounds_hold_on_every_input():
    rng = random.Random(13)
    ops = ("and", "or", "xor", "add", "not")
    decided = 0
    for _ in range(300):
        e = random_expr(rng, width=8, depth=3, ops=ops)
        for op in ("eq", "ne", "ult", "ule", "ugt", "uge"):
            c = Const(rng.choice([0, 1, 0x7F, 0x80, 0xFF, rng.getrandbits(8)]), 8)
            for a, b in ((e, c), (c, e)):
                value = decided_comparison(op, a, b)
                if value is None:
                    continue
                decided += 1
                for env in ({"a": i, "b": rng.getrandbits(8), "c": rng.getrandbits(8)} for i in range(256)):
                    assert evaluate(BinOp(op, a, b, 1), env) == value, (op, a, b)
    assert decided > 50


def test_upper_bound_of_a_division_by_a_variable():
    assert upper_bound(parse("udiv(2, y * y + 1)", 32)) == 2
    assert upper_bound(parse("urem(x, 10)", 32)) == 9
    assert upper_bound(parse("x & 0x30", 32)) == 0x30
