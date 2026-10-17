"""Division and remainder in the IR: total semantics, ARM64's.

x / 0 = 0, x % 0 = x, MIN / -1 = MIN, MIN % -1 = 0; signed division rounds toward zero and the
remainder takes the dividend's sign. Lifted divisions never meet the zero or overflow cases (a trap
proof comes first); with these values narrowing is an exact identity everywhere.
"""

import itertools
import random

import pytest
import z3

from binja_d810.ir.bits import possible_ones
from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import DIV_OPS, BinOp, Const, UnOp, Var, is_mba, mask, to_signed, to_str
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import to_z3

WIDTHS = (8, 16, 32, 64)


def reference(op: str, a: int, b: int, w: int) -> int:
    m = mask(w)
    if op == "udiv":
        return 0 if b == 0 else a // b
    if op == "urem":
        return a if b == 0 else a % b
    sa, sb = to_signed(a, w), to_signed(b, w)
    if sb == 0:
        return 0 if op == "sdiv" else a
    q = abs(sa) // abs(sb) * (1 if (sa < 0) == (sb < 0) else -1)
    return q & m if op == "sdiv" else (sa - q * sb) & m


def values(w: int) -> list[int]:
    m, top = mask(w), 1 << (w - 1)
    edges = [0, 1, 2, 3, 7, m, m - 1, top, top - 1, top + 1]
    rng = random.Random(w)
    return edges + [rng.getrandbits(w) for _ in range(12)]


def test_division_ops_are_binary_ops():
    assert DIV_OPS == {"udiv", "urem", "sdiv", "srem"}
    with pytest.raises(ValueError):
        BinOp("udiv", Var("x", 32), Var("y", 16), 32)


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("op", sorted(DIV_OPS))
def test_semantics_match_the_reference(op, w):
    for a, b in itertools.product(values(w), repeat=2):
        assert evaluate(BinOp(op, Const(a, w), Const(b, w), w), {}) == reference(op, a, b, w), (op, a, b)


def test_the_defined_cases():
    w, top = 32, 1 << 31
    assert evaluate(BinOp("udiv", Const(7, w), Const(0, w), w), {}) == 0
    assert evaluate(BinOp("urem", Const(7, w), Const(0, w), w), {}) == 7
    assert evaluate(BinOp("sdiv", Const(top, w), Const(mask(w), w), w), {}) == top  # MIN / -1 = MIN
    assert evaluate(BinOp("srem", Const(top, w), Const(mask(w), w), w), {}) == 0
    assert evaluate(BinOp("sdiv", Const(-7 & mask(w), w), Const(2, w), w), {}) == -3 & mask(w)  # toward zero
    assert evaluate(BinOp("srem", Const(-7 & mask(w), w), Const(2, w), w), {}) == -1 & mask(w)  # dividend's sign


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("op", sorted(DIV_OPS))
def test_z3_agrees_with_evaluation(op, w):
    ctx = z3.Context()
    for a, b in itertools.product(values(w)[:12], repeat=2):
        e = BinOp(op, Const(a, w), Const(b, w), w)
        got = z3.simplify(to_z3(e, {}, ctx)).as_long()
        assert got == evaluate(e, {}), (op, a, b)


def test_z3_agrees_on_variables():
    # Not only on constants: the solver's view of x / 0 and MIN / -1 is the evaluator's.
    ctx = z3.Context()
    x, y = Var("x", 16), Var("y", 16)
    zv = {"x": z3.BitVec("x", 16, ctx), "y": z3.BitVec("y", 16, ctx)}
    for op in sorted(DIV_OPS):
        e = BinOp(op, x, y, 16)
        for a, b in [(5, 0), (0x8000, 0xffff), (0xfff9, 2), (9, 4)]:
            s = z3.Solver(ctx=ctx)
            s.add(zv["x"] == a, zv["y"] == b, to_z3(e, zv, ctx) != evaluate(e, {"x": a, "y": b}))
            assert s.check() == z3.unsat, (op, a, b)


@pytest.mark.parametrize("op,ext", [("udiv", "zx"), ("urem", "zx"), ("sdiv", "sx"), ("srem", "sx")])
def test_narrowing_is_exact_at_every_value(op, ext):
    x, y = Var("x", 8), Var("y", 8)
    wide = UnOp("trunc", BinOp(op, UnOp(ext, x, 16), UnOp(ext, y, 16), 16), 8)
    narrow = BinOp(op, x, y, 8)
    assert normalize(wide) == narrow
    for a, b in itertools.product(range(256), repeat=2):
        env = {"x": a, "y": b}
        assert evaluate(wide, env) == evaluate(narrow, env), (op, a, b)


def test_narrowing_takes_a_constant_that_fits():
    x = Var("x", 32)
    wide = UnOp("trunc", BinOp("udiv", UnOp("zx", x, 64), Const(7, 64), 64), 32)
    assert normalize(wide) == BinOp("udiv", x, Const(7, 32), 32)
    signed = UnOp("trunc", BinOp("sdiv", UnOp("sx", x, 64), Const(-7 & mask(64), 64), 64), 32)
    assert normalize(signed) == BinOp("sdiv", x, Const(-7 & mask(32), 32), 32)
    # 2**32 does not fit: the quotient is not the narrow one.
    big = UnOp("trunc", BinOp("udiv", UnOp("zx", x, 64), Const(1 << 32, 64), 64), 32)
    assert normalize(big) != BinOp("udiv", x, Const(0, 32), 32)


def test_known_bits_bound_unsigned_division_and_remainder():
    y = Var("y", 32)
    assert possible_ones(BinOp("udiv", Const(2, 32), y, 32)) == 3  # 2 / y is 0, 1 or 2
    low = BinOp("and", Var("x", 8), Const(0x0F, 8), 8)
    for op in ("udiv", "urem"):
        e = BinOp(op, low, Var("y", 8), 8)
        bound = possible_ones(e)
        assert bound == 0x0F
        for a, b in itertools.product(range(256), repeat=2):
            assert evaluate(e, {"x": a, "y": b}) & ~bound == 0
    assert possible_ones(BinOp("sdiv", Const(2, 8), Var("y", 8), 8)) == 0xFF


def test_printing_and_parsing_round_trip():
    e = BinOp("udiv", Const(2, 32), BinOp("add", BinOp("mul", Var("x", 32), Var("x", 32), 32), Const(1, 32), 32), 32)
    text = to_str(e)
    assert text == "udiv(2, ((x * x) + 1))"
    assert parse(text, 32, {"x": 32}) == e
    for op in sorted(DIV_OPS):
        assert parse(f"{op}(x, y)", 32, {"x": 32, "y": 32}) == BinOp(op, Var("x", 32), Var("y", 32), 32)


def test_division_is_neither_arithmetic_nor_bitwise_for_mba():
    # A quotient of a bitwise value is not something Tier 2 rewrites (as rotates).
    e = BinOp("udiv", BinOp("and", Var("x", 32), Var("y", 32), 32), Const(3, 32), 32)
    assert not is_mba(e)
