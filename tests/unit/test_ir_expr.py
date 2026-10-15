import random

import pytest

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import (
    BinOp, Const, UnOp, Var, is_mba, mask, node_count, substitute, to_str, variables,
)

a, b = Var("a", 32), Var("b", 32)
M32 = mask(32)


def mba_add(x, y):  # (x ^ y) + 2*(x & y)
    return BinOp("add", BinOp("xor", x, y, 32), BinOp("mul", Const(2, 32), BinOp("and", x, y, 32), 32), 32)


def test_const_is_masked_to_width():
    assert Const(-1, 8).value == 0xFF
    assert Const(0x1FF, 8).value == 0xFF


def test_binop_rejects_mismatched_widths():
    with pytest.raises(ValueError):
        BinOp("add", Var("a", 32), Var("b", 64), 32)


def test_shift_amount_may_have_another_width():
    BinOp("shl", a, Const(3, 8), 32)


def test_unop_width_rules():
    UnOp("zx", Var("p", 8), 32)
    with pytest.raises(ValueError):
        UnOp("zx", a, 8)
    with pytest.raises(ValueError):
        UnOp("trunc", Var("p", 8), 32)
    with pytest.raises(ValueError):
        UnOp("frob", a, 32)


def test_evaluate_classic_mba_identity():
    rng = random.Random(1)
    for _ in range(200):
        x, y = rng.getrandbits(32), rng.getrandbits(32)
        assert evaluate(mba_add(a, b), {"a": x, "b": y}) == (x + y) & M32


def test_evaluate_shifts_and_extensions():
    assert evaluate(BinOp("ashr", Const(0x80000000, 32), Const(4, 32), 32), {}) == 0xF8000000
    assert evaluate(BinOp("lshr", Const(0x80000000, 32), Const(4, 32), 32), {}) == 0x08000000
    assert evaluate(BinOp("shl", Const(1, 32), Const(33, 32), 32), {}) == 2  # amount mod width
    assert evaluate(UnOp("sx", Const(0x80, 8), 32), {}) == 0xFFFFFF80
    assert evaluate(UnOp("zx", Const(0x80, 8), 32), {}) == 0x80
    assert evaluate(UnOp("trunc", Const(0x12345678, 32), 8), {}) == 0x78


def test_variables_dedupe_in_first_occurrence_order():
    e = BinOp("add", b, BinOp("xor", a, b, 32), 32)
    assert [v.name for v in variables(e)] == ["b", "a"]


def test_node_count_and_is_mba():
    assert node_count(mba_add(a, b)) == 9
    assert is_mba(mba_add(a, b))
    assert not is_mba(BinOp("add", a, b, 32))
    assert not is_mba(BinOp("xor", a, b, 32))


def test_to_str_is_fully_parenthesised():
    assert to_str(mba_add(a, b)) == "((a ^ b) + (2 * (a & b)))"
    assert to_str(BinOp("add", a, Const(-5, 32), 32)) == "(a + (-5))"
    assert to_str(UnOp("zx", Var("p", 32), 64)) == "zx64(p)"
    assert to_str(BinOp("ashr", a, Const(3, 32), 32)) == "sar(a, 3)"
    assert to_str(Const(0xDEADBEEF, 32)) == "0xdeadbeef"


def test_var_ref_is_ignored_by_equality():
    assert Var("a", 32, ref=object()) == Var("a", 32)


def test_substitute_replaces_by_name():
    e = substitute(BinOp("xor", a, b, 32), {"a": Var("x", 32)})
    assert e == BinOp("xor", Var("x", 32), b, 32)
