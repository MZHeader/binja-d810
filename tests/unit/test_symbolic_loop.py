import pytest
from binja_d810.control.symbolic_loop import summarize
from binja_d810.control.finite import Loop,Decision,Break,Continue
from binja_d810.control.equivalence import Assignment
from binja_d810.ir.expr import Var,Const,BinOp
from binja_d810.ir.evaluate import evaluate


@pytest.mark.parametrize('width',(8,16,32,64))
def test_symbolic_state_matches_an_independent_loop(width):
    i,s,x=(Var(n,width) for n in ('i','s','x'))
    loop=Loop(Const(1,1),(Assignment(s,BinOp('mul',BinOp('xor',s,x,width),Const(13,width),width)),
                        Decision(BinOp('eq',i,Const(7,width),1),(Break(),)),
                        Assignment(i,BinOp('add',i,Const(1,width),width))))
    summary=summarize(loop,{'i':Const(0,width),'s':Const(9,width)},iterations=8,max_nodes=2000,tick=lambda:None)
    assert summary.iterations==8
    for n in range(256):
        expected=9
        for _ in range(8):expected=((expected^n)*13)&((1<<width)-1)
        assert evaluate(summary.values['s'],{'x':n})==expected


def test_unknown_control_and_insufficient_bounds_are_rejected():
    x=Var('x',8)
    with pytest.raises(ValueError,match='unknown'):
        summarize(Loop(BinOp('ne',x,Const(0,8),1),()),{},iterations=8,max_nodes=2000,tick=lambda:None)
    with pytest.raises(ValueError,match='iteration limit'):
        summarize(Loop(Const(1,1),(Continue(),)),{},iterations=8,max_nodes=2000,tick=lambda:None)


def test_symbolic_expression_growth_is_bounded():
    x=Var('x',8)
    with pytest.raises(ValueError,match='expression limit'):
        summarize(Loop(Const(1,1),(Assignment(x,BinOp('add',x,x,8)),)),{},iterations=8,max_nodes=4,tick=lambda:None)
