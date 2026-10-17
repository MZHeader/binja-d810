import itertools
import random

import pytest
import z3

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import CMP_OPS, BinOp, Const, UnOp, Var, is_candidate, mask, to_str
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import to_z3


def reference(op, a, b, w):
    sa = a - (1 << w) if a >> (w - 1) else a
    sb = b - (1 << w) if b >> (w - 1) else b
    return int({"eq": a == b, "ne": a != b, "ult": a < b, "ule": a <= b, "ugt": a > b, "uge": a >= b,
                "slt": sa < sb, "sle": sa <= sb, "sgt": sa > sb, "sge": sa >= sb}[op])


@pytest.mark.parametrize("width", [8, 16, 32, 64])
@pytest.mark.parametrize("op", sorted(CMP_OPS))
def test_comparisons_evaluate_like_the_reference_at_the_boundaries(op, width):
    m = mask(width)
    edges = [0, 1, m, m >> 1, (m >> 1) + 1, 2, m - 1]
    for a, b in itertools.product(edges, edges):
        e = BinOp(op, Const(a, width), Const(b, width), 1)
        assert evaluate(e, {}) == reference(op, a, b, width), (op, a, b)


@pytest.mark.parametrize("op", sorted(CMP_OPS))
def test_comparisons_in_z3_agree_with_evaluate(op):
    x, y = Var("x", 16), Var("y", 16)
    e = BinOp(op, x, y, 1)
    zx_, zy = z3.BitVec("x", 16), z3.BitVec("y", 16)
    rng = random.Random(3)
    for _ in range(200):
        a, b = rng.getrandbits(16), rng.getrandbits(16)
        s = z3.Solver()
        s.add(zx_ == a, zy == b)
        assert s.check() == z3.sat
        assert s.model().eval(to_z3(e, {"x": zx_, "y": zy})).as_long() == evaluate(e, {"x": a, "y": b})


def test_comparisons_have_one_bit_and_equal_operand_widths():
    with pytest.raises(ValueError):
        BinOp("eq", Var("x", 32), Var("y", 16), 1)
    with pytest.raises(ValueError):
        BinOp("eq", Var("x", 32), Var("y", 32), 32)
    assert BinOp("ult", Var("x", 8), Var("y", 8), 1).width == 1


def test_comparisons_print_and_parse():
    e = BinOp("slt", BinOp("and", Var("x", 32), Const(7, 32), 32), Const(8, 32), 1)
    assert to_str(e) == "slt((x & 7), 8)"
    assert parse("slt(x & 7, 8)", 1, {"x": 32}) == e
    assert parse("zx32(ne(x & 1, 0))", 32) == UnOp("zx", BinOp("ne", parse("x & 1", 32), Const(0, 32), 1), 32)


def test_normalization_of_comparisons():
    x = Var("x", 32)
    assert normalize(BinOp("eq", x, x, 1)) == Const(1, 1)
    assert normalize(BinOp("ult", x, x, 1)) == Const(0, 1)
    assert normalize(BinOp("ult", x, Const(0, 32), 1)) == Const(0, 1)
    assert normalize(BinOp("uge", x, Const(0, 32), 1)) == Const(1, 1)
    # bool_to_int of a 0/1 test is the value (v2.1's lifting rule, now an identity)
    t = normalize(parse("x & 1", 32))
    assert normalize(UnOp("zx", BinOp("ne", t, Const(0, 32), 1), 32)) == t
    assert normalize(UnOp("zx", BinOp("eq", t, Const(0, 32), 1), 32)) == normalize(BinOp("xor", t, Const(1, 32), 32))


def test_candidates_are_mbas_or_comparisons_over_bitwise_operations_or_products():
    assert is_candidate(parse("(x ^ y) + 2 * (x & y)", 32))
    assert is_candidate(parse("eq((x * (x + 1)) & 1, 0)", 1, {"x": 32}))
    assert is_candidate(parse("ne(x | 16, 15)", 1, {"x": 32}))
    assert is_candidate(parse("eq(x * y, 3)", 1, {"x": 32, "y": 32}))
    assert not is_candidate(parse("ult(x, y - 1)", 1, {"x": 32, "y": 32}))
    assert not is_candidate(parse("x + 1", 32))


def test_comparisons_decided_by_their_form_or_bit_bounds_are_candidates():
    # Compilers reduce opaque predicates to these: a copy makes both operands one variable
    # (opaque_mba_or at -O2), and (x & 0xff) <= 0xff becomes a zero-extended low byte.
    assert is_candidate(parse("eq(x, x)", 1, {"x": 32}))
    assert is_candidate(parse("ugt(zx32(trunc8(x)), 0xff)", 1, {"x": 32}))
    assert is_candidate(parse("uge(zx32(c), 0x100)", 1, {"c": 8}))
    assert is_candidate(parse("eq(zx32(c), 0x100)", 1, {"c": 8}))
    assert not is_candidate(parse("eq(zx32(c), 0x41)", 1, {"c": 8}))
    assert not is_candidate(parse("ult(zx32(c), 0x80)", 1, {"c": 8}))
    assert not is_candidate(parse("eq(x, y)", 1, {"x": 32, "y": 32}))
