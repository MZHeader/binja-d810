from binja_d810.semantic.project_preimage import project
from binja_d810.ir.expr import Var,Const,BinOp
from binja_d810.deadline import Deadline
from binja_d810.semantic.verify import check_equivalent,Verdict


def test_bit_fields_project_only_when_the_input_is_unique():
    x=Var('x',32)
    def field(m,c):return BinOp('eq',BinOp('and',x,Const(m,32),32),Const(c,32),1)
    low=field(65535,0x1234);high=field(0xffff0000,0x87650000)
    predicate=BinOp('and',low,high,1)
    result=project(predicate,proof_ms=200,deadline=Deadline.after(2))
    assert result==BinOp('eq',x,Const(0x87651234,32),1)
    assert check_equivalent(predicate,result) is Verdict.PROVEN
    assert project(low,proof_ms=200,deadline=Deadline.after(2))==low


def test_expired_projection_does_not_guess_an_input():
    x=Var('x',8)
    predicate=BinOp('eq',BinOp('and',x,Const(7,8),8),Const(3,8),1)
    assert project(predicate,proof_ms=200,deadline=Deadline.after(0))==predicate


def test_partial_fields_project_without_constraining_unknown_upper_bits():
    x=Var('x',32)
    a=BinOp('eq',BinOp('and',x,Const(255,32),32),Const(52,32),1)
    b=BinOp('eq',BinOp('and',BinOp('lshr',x,Const(8,32),32),Const(255,32),32),Const(18,32),1)
    predicate=BinOp('and',a,b,1)
    result=project(predicate,proof_ms=200,deadline=Deadline.after(2))
    assert result==BinOp('eq',BinOp('and',x,Const(65535,32),32),Const(0x1234,32),1)
    assert check_equivalent(predicate,result) is Verdict.PROVEN
