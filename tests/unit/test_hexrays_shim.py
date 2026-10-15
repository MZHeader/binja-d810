from binja_d810.ir.expr import Const, UnOp, Var
from binja_d810.third_party.d810.hexrays.hexrays_helpers import equal_bnot_mop, equal_mops_ignore_size

a = Var("a", 32)


def test_equal_mops():
    assert equal_mops_ignore_size(Const(5, 32), Const(5, 64))
    assert equal_mops_ignore_size(a, Var("a", 32))


def test_equal_bnot():
    assert equal_bnot_mop(Const(0xFFFFFFF0, 32), Const(0xF, 32))
    assert equal_bnot_mop(UnOp("not", a, 32), a)
    assert not equal_bnot_mop(a, a)
