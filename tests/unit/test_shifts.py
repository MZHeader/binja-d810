"""IR shift semantics (spec section 6.5): match Binary Ninja's IL, with no result-width modulo.

Binary Ninja's own dataflow, measured on assembly fixtures (tests/samples/src/shifts_*.S, pinned in
tests/binja/test_shift_reference.py), takes the count modulo max(32, width) and then loses the bits
shifted past the width:
- 8/16-bit: x86 `shl al, cl` by 8 or 9, `shr ax, cl` by 16 and `shl ax, 17` all give 0 (no modulo 8/16);
- 32/64-bit: ARM64 `lsl w0, w0, w1` by 33 gives 2 and `asr`/`lsr` by 32 leave the value unchanged,
  64-bit by 65 shifts by 1. ARM64's lifter emits no count mask; the hardware takes the count modulo
  the register width (LSLV/LSRV/ASRV), which this matches.
x86's own 5/6-bit count masks are explicit `and` nodes in the IL and are lifted as such.

The reference below is written from that definition on unbounded integers, independently of
binja_d810.ir.evaluate and binja_d810.semantic.verify.
"""

import pytest
import z3

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import BinOp, Const, Var
from binja_d810.semantic.verify import Verdict, check_equivalent, to_z3

WIDTHS = (8, 16, 32, 64)


def reference(op: str, x: int, count: int, w: int) -> int:
    m = (1 << w) - 1
    x &= m
    c = count % max(32, w)
    if op == "shl":
        return (x * 2 ** c) & m
    if op == "lshr":
        return x // 2 ** c
    signed = x - (1 << w) if x >> (w - 1) else x
    return (signed // 2 ** c) & m  # floor division rounds toward -inf, like an arithmetic shift


def counts(w: int) -> list[int]:
    return [0, 1, w - 1, w, w + 1, 2 * w, 31, 32, 33, 63, 64, 65, (1 << w) - 1]


def values(w: int) -> list[int]:
    m = (1 << w) - 1
    return [0, 1, 0x5A & m, m, m >> 1, (m >> 1) + 1]


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("op", ["shl", "lshr", "ashr"])
def test_evaluate_matches_reference_at_boundaries(op, w):
    for x in values(w):
        for c in counts(w):
            got = evaluate(BinOp(op, Const(x, w), Const(c, w), w), {})
            assert got == reference(op, x, c, w), (op, w, hex(x), c)


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("op", ["shl", "lshr", "ashr"])
def test_z3_matches_reference_at_boundaries(op, w):
    xs, cs = z3.BitVec("x", w), z3.BitVec("c", w)
    term = to_z3(BinOp(op, Var("x", w), Var("c", w), w), {"x": xs, "c": cs})
    for x in values(w):
        for c in counts(w):
            value = z3.simplify(z3.substitute(term, (xs, z3.BitVecVal(x, w)), (cs, z3.BitVecVal(c, w))))
            assert value.as_long() == reference(op, x, c, w), (op, w, hex(x), c)


@pytest.mark.parametrize("op", ["shl", "lshr", "ashr"])
def test_narrow_count_operand(op):
    # Binary Ninja often gives the count its own (narrower) width: cl in shl al, cl.
    for x in values(32):
        for c in (0, 31, 32, 33, 255):
            e = BinOp(op, Const(x, 32), Const(c, 8), 32)
            assert evaluate(e, {}) == reference(op, x, c, 32)


@pytest.mark.parametrize("op", ["shl", "lshr", "ashr"])
def test_wide_count_operand_is_not_truncated(op):
    # An 8-bit shift by a 16-bit count: the count is reduced modulo 32 from its full value, so
    # 256 is a shift by 0 and 264 a shift by 8 (everything out); truncating the count to 8 bits
    # first would get 264 wrong.
    xs, cs = z3.BitVec("x", 8), z3.BitVec("c", 16)
    e = BinOp(op, Var("x", 8), Var("c", 16), 8)
    term = to_z3(e, {"x": xs, "c": cs})
    for x in values(8):
        for c in (0, 7, 8, 255, 256, 257, 264, 0xFFFF):
            expected = reference(op, x, c, 8)
            assert evaluate(e, {"x": x, "c": c}) == expected, (op, x, c)
            value = z3.simplify(z3.substitute(term, (xs, z3.BitVecVal(x, 8)), (cs, z3.BitVecVal(c, 16))))
            assert value.as_long() == expected, (op, x, c)


def test_x86_shl_al_cl_by_8_is_zero():
    # Intel SDM: SHL r/m8, CL masks the count to 5 bits (8 stays 8), and shifting an 8-bit
    # destination left by 8 clears it. Binary Ninja lifts the mask explicitly: al << (cl & 0x1f).
    al, cl = Var("al", 8), Var("cl", 8)
    lifted = BinOp("shl", al, BinOp("and", cl, Const(0x1F, 8), 8), 8)
    assert evaluate(lifted, {"al": 1, "cl": 8}) == 0
    assert evaluate(lifted, {"al": 1, "cl": 40}) == 0  # 40 & 0x1f == 8
    assert evaluate(lifted, {"al": 1, "cl": 33}) == 2  # 33 & 0x1f == 1


def test_arm64_lsl_w_by_33_is_a_shift_by_one():
    # ARM ARM, LSLV (32-bit): shift = UInt(Wm) MOD 32. Binary Ninja lifts it as w0 << w1, no mask.
    w0, w1 = Var("w0", 32), Var("w1", 32)
    lifted = BinOp("shl", w0, w1, 32)
    assert evaluate(lifted, {"w0": 1, "w1": 33}) == 2
    assert evaluate(BinOp("ashr", w0, w1, 32), {"w0": 0x80000000, "w1": 32}) == 0x80000000


def test_shift_identities_hold_under_these_semantics():
    x, c = Var("x", 32), Var("c", 32)
    assert check_equivalent(BinOp("shl", x, BinOp("and", c, Const(31, 32), 32), 32),
                            BinOp("shl", x, c, 32)) is Verdict.PROVEN
    assert check_equivalent(BinOp("shl", x, c, 32), Const(0, 32)) is Verdict.REFUTED
