import pytest
from binja_d810.ir.expr import BinOp,Const,Var
from binja_d810.deadline import Deadline
from binja_d810.semantic.constant_guard import prove


@pytest.mark.parametrize('width',(8,16,32,64))
def test_exact_identity_guard(width):
    x=Var('x',width)
    guard=BinOp('eq',BinOp('xor',x,x,width),Const(0,width),1)
    assert prove(guard,proof_ms=200,deadline=Deadline.after(1))==Const(1,1)


def test_unknown_non_boolean_and_expired_guards_stay():
    x=Var('x',32)
    assert prove(BinOp('eq',x,Const(0,32),1),proof_ms=200,deadline=Deadline.after(1)) is None
    assert prove(Const(1,32),proof_ms=200,deadline=Deadline.after(1)) is None
    assert prove(Const(1,1),proof_ms=200,deadline=Deadline.after(0)) is None
