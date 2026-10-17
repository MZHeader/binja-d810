"""Can a division trap?: a division is lifted only when no input makes it trap."""

import pytest

from binja_d810.ir.expr import BinOp, Const, Var, mask
from binja_d810.ir.parse import parse
from binja_d810.semantic import traps
from binja_d810.semantic.traps import never_traps

W = {"x": 32, "y": 32}


def e(text: str, width: int = 32):
    return parse(text, width, {name: width for name in W})


# x * x + 3 is never 0: -3 is 5 modulo 8, and squares are 0, 1 or 4 modulo 8.
@pytest.mark.parametrize("divisor", ["(x * x) + 1", "x | 1", "7", "(x & 255) + 1", "(x * (x + 1)) | 4", "(x * x) + 3"])
def test_unsigned_divisors_that_are_never_zero(divisor):
    assert never_traps("udiv", Const(2, 32), e(divisor))
    assert never_traps("urem", e("y"), e(divisor))


@pytest.mark.parametrize("divisor", ["x", "x << 2", "x & 1", "0", "x * x", "(x * x) - 1"])
def test_unsigned_divisors_that_can_be_zero(divisor):
    assert not never_traps("udiv", Const(2, 32), e(divisor))


def test_signed_minimum_by_minus_one_traps():
    # x | 1 is never zero, but is -1 for x = -1: MIN / -1 overflows (x86 idiv traps).
    assert not never_traps("sdiv", e("y"), e("x | 1"))
    assert not never_traps("srem", e("y"), e("x | 1"))
    assert not never_traps("sdiv", e("y"), Const(mask(32), 32))
    # A dividend that is never the minimum, or a divisor that is never -1, cannot overflow.
    assert never_traps("sdiv", e("y >> 1"), e("x | 1"))
    assert never_traps("sdiv", e("y"), e("(x & 255) + 1"))
    assert never_traps("sdiv", Const(5, 32), Const(mask(32), 32))
    assert never_traps("srem", e("y"), Const(3, 32))
    assert not never_traps("sdiv", e("y"), Const(0, 32))


def test_wide_divisors():
    x = Var("x", 64)
    assert never_traps("udiv", Const(2, 64), BinOp("add", BinOp("mul", x, x, 64), Const(1, 64), 64))
    assert not never_traps("udiv", Const(2, 64), x)


def test_verdicts_are_remembered_by_shape(monkeypatch):
    assert never_traps("udiv", Const(2, 32), e("(x * x) + 1"))

    def no_solver(*args, **kwargs):
        raise AssertionError("the solver ran again for the same shape")

    monkeypatch.setattr(traps, "_solve", no_solver)
    renamed = parse("(z * z) + 1", 32, {"z": 32})
    assert never_traps("udiv", Const(9, 32), renamed)  # unsigned: the dividend does not matter


def test_an_inconclusive_proof_is_not_remembered(monkeypatch):
    divisor = parse("(q * q) + 5", 32, {"q": 32})  # -5 is 3 modulo 8: never a square, never 0
    real = traps._solve
    monkeypatch.setattr(traps, "_solve", lambda *args, **kwargs: None)  # a timeout
    assert not never_traps("udiv", Const(2, 32), divisor)
    monkeypatch.setattr(traps, "_solve", real)
    assert never_traps("udiv", Const(2, 32), divisor)
