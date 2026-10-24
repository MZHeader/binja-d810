import pytest
from dataclasses import replace
from binja_d810.flags.recombine import Limits, recombine
from binja_d810.flags.boolean import simplify
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import BinOp, Const, UnOp, Var, tree_size
from binja_d810.semantic.verify import Verdict, check_equivalent


@pytest.mark.parametrize('width',(8,16,32,64))
def test_widened_boolean_arithmetic_becomes_a_boolean_conjunction(width):
    a=BinOp('ult',Var('x',width),Var('y',width),1)
    b=BinOp('ne',Var('y',width),Const(7,width),1)
    av,bv=UnOp('zx',a,width),UnOp('zx',b,width)
    encoded=BinOp('and',BinOp('xor',av,BinOp('xor',bv,Const(1,width),width),width),av,width)
    source=BinOp('ne',BinOp('and',encoded,Const(1,width),width),Const(0,width),1)
    found=recombine(source)
    expected=BinOp('and',a,b,1)
    assert tree_size(found.expression) <= tree_size(expected)
    assert check_equivalent(source,found.expression) is Verdict.PROVEN


def test_boolean_caps_deadline_and_disable_keep_the_original():
    a,b=Var('a',1),Var('b',1)
    source=BinOp('ne',BinOp('add',UnOp('zx',a,32),UnOp('zx',b,32),32),Const(0,32),1)
    for limits in (Limits(boolean=False),Limits(boolean_atoms=1),Limits(work=1)):
        assert recombine(source,limits=limits).expression==source
    assert recombine(source,deadline=Deadline.after(0)).expression==source


def test_a_wide_unconstrained_input_is_not_assumed_boolean():
    source=BinOp('ne',BinOp('add',Var('x',32),Var('y',32),32),Const(0,32),1)
    assert simplify(source,max_atoms=4,max_nodes=2000,proof_ms=200,deadline=Deadline.never(),tick=lambda:None) is None


@pytest.mark.parametrize('extension',('zx','sx'))
@pytest.mark.parametrize('width',(8,16,32,64))
def test_integer_arithmetic_and_signed_boolean_extensions_are_exact(extension,width):
    p,q=Var('p',1),Var('q',1)
    a,b=UnOp(extension,p,width),UnOp(extension,q,width)
    source=BinOp('ne',BinOp('add',a,b,width),Const(0,width),1)
    found=recombine(source)
    assert tree_size(found.expression)<tree_size(source)
    assert check_equivalent(source,found.expression) is Verdict.PROVEN


def test_boolean_proof_abstracts_large_predicate_operands_without_assuming_their_values(monkeypatch):
    from binja_d810.flags import boolean
    from binja_d810.ir.expr import variables
    original=boolean.check;seen=[]
    def checked(a,b,**kwargs):
        seen.append((a,b))
        assert all(v.width==1 for v in variables(a)+variables(b))
        return original(a,b,**kwargs)
    monkeypatch.setattr(boolean,'check',checked)
    x,y=Var('x',64),Var('y',64)
    p=BinOp('eq',BinOp('mul',x,x,64),Const(49,64),1)
    q=BinOp('ult',BinOp('mul',x,y,64),Const(127,64),1)
    source=BinOp('ne',BinOp('add',UnOp('zx',p,64),UnOp('zx',q,64),64),Const(0,64),1)
    found=boolean.simplify(source,max_atoms=4,max_nodes=2000,proof_ms=200,deadline=Deadline.never(),tick=lambda:None)
    assert found is not None and seen
    assert check_equivalent(source,found) is Verdict.PROVEN
