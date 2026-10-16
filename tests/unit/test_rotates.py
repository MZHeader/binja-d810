"""Rotates in the IR (v2.1 Task 6): count modulo the width, as Binary Ninja's dataflow folds them
(x86 rol al by 9 rotates by 1; ARM64 ror w by 33 and ror x by 65 rotate by 1)."""

import pytest
import z3

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import BinOp, Const, Var, is_mba
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import Verdict, check_equivalent, to_z3


def reference(op, x, count, w):
    m = (1 << w) - 1
    c = count % w
    x &= m
    left = ((x << c) | (x >> (w - c))) & m if c else x
    return left if op == "rol" else (((x >> c) | (x << (w - c))) & m if c else x)


@pytest.mark.parametrize("w", [8, 16, 32, 64])
@pytest.mark.parametrize("op", ["rol", "ror"])
def test_evaluator_and_z3_match_the_reference(op, w):
    xs, cs = z3.BitVec("x", w), z3.BitVec("c", 8)
    e = BinOp(op, Var("x", w), Var("c", 8), w)
    term = to_z3(e, {"x": xs, "c": cs})
    for x in (0, 1, 0x81, (1 << w) - 1, 1 << (w - 1)):
        for c in (0, 1, w - 1, w, w + 1, 2 * w + 3, 255):
            expected = reference(op, x, c, w)
            assert evaluate(e, {"x": x, "c": c}) == expected, (op, w, x, c)
            got = z3.simplify(z3.substitute(term, (xs, z3.BitVecVal(x, w)), (cs, z3.BitVecVal(c, 8)))).as_long()
            assert got == expected, (op, w, x, c)


def test_rotates_are_neither_arithmetic_nor_bitwise():
    assert not is_mba(parse("rol(x, 3) + y", 32)) and is_mba(parse("rol(x ^ y, 3) + (x & y)", 32))


def test_rotate_identities_are_proven():
    assert check_equivalent(parse("rol(x, 3)", 32), parse("ror(x, 29)", 32)) is Verdict.PROVEN
    assert check_equivalent(parse("rol(x, 3)", 32), BinOp("or", BinOp("shl", Var("x", 32), Const(3, 32), 32),
                                                          BinOp("lshr", Var("x", 32), Const(29, 32), 32), 32)) is Verdict.PROVEN
