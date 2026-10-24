import random
import pytest
from binja_d810.ir.expr import BinOp,Const,UnOp,Var,mask
from binja_d810.ir.evaluate import evaluate
from binja_d810.semantic.preimage import comparison
from binja_d810.semantic.verify import check_equivalent,Verdict


def simplify(expr):
    return comparison(expr,steps=64,max_nodes=2000,tick=lambda:None)


@pytest.mark.parametrize('width',(8,16,32,64))
@pytest.mark.parametrize('op',('eq','ne'))
def test_random_reversible_encodings(width,op):
    rng=random.Random(width)
    x=Var('x',width)
    for _ in range(12):
        e=x
        for operation in ('mul','add','xor','rol','sub'):
            k=rng.getrandbits(width)
            if operation=='mul':k|=1
            e=BinOp(operation,e,Const(k,width),width)
        target=evaluate(e,{'x':rng.getrandbits(width)})
        expr=BinOp(op,e,Const(target,width),1)
        result=simplify(expr)
        assert result is not None and result.steps==5
        assert check_equivalent(expr,result.expression) is Verdict.PROVEN


@pytest.mark.parametrize('operation',('mul','shl','lshr'))
def test_noninvertible_encodings_are_exact_for_every_byte(operation):
    x=Var('x',8)
    for k in (0,1,2,4,6,8,32,254):
        for target in (0,1,2,7,128,254,255):
            source=BinOp('eq',BinOp(operation,x,Const(k,8),8),Const(target,8),1)
            found=simplify(source)
            if found is not None:
                assert all(evaluate(source,{'x':v})==evaluate(found.expression,{'x':v}) for v in range(256))


@pytest.mark.parametrize('op',('zx','sx','trunc','neg','not'))
def test_casts_and_unary_preimages(op):
    x=Var('x',8 if op in ('zx','sx') else 32)
    width=32 if op in ('zx','sx','neg','not') else 8
    for c in (0,1,127,128,255,0xffffffff):
        expr=BinOp('ne',UnOp(op,x,width),Const(c,width),1)
        found=simplify(expr)
        if found is not None:assert check_equivalent(expr,found.expression) is Verdict.PROVEN


def test_ordering_unknown_constants_and_limits_are_not_guessed():
    x,y=Var('x',32),Var('y',32)
    e=BinOp('mul',x,Const(7,32),32)
    assert simplify(BinOp('ult',e,Const(5,32),1)) is None
    assert simplify(BinOp('eq',e,y,1)) is None
    expr=BinOp('eq',e,Const(5,32),1)
    assert comparison(expr,steps=0,max_nodes=20,tick=lambda:None) is None
    def stop():raise ValueError('deadline')
    with pytest.raises(ValueError,match='deadline'):comparison(expr,steps=4,max_nodes=20,tick=stop)
