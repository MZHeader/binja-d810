import random
import pytest
from binja_d810.ir.known import known_bits
from binja_d810.ir.expr import Var,Const,BinOp,UnOp,mask
from binja_d810.ir.evaluate import evaluate
from binja_d810.semantic.verify import check_equivalent,Verdict


@pytest.mark.parametrize('width',(8,16,32,64))
def test_fixed_low_fields_survive_addition_to_unknown_high_bits(width):
    x=Var('x',width)
    word=BinOp('add',BinOp('mul',x,Const(1<<(width//2),width),width),Const(37,width),width)
    byte=BinOp('and',word,Const(mask(width//2),width),width)
    value,known=known_bits(byte)
    assert known==mask(width) and value==37&mask(width//2)
    assert check_equivalent(byte,Const(value,width)) is Verdict.PROVEN


def test_every_reported_bit_is_sound_for_random_byte_expressions():
    rng=random.Random(351)
    x=Var('x',8)
    for _ in range(300):
        expr=x
        for _ in range(8):
            op=rng.choice(('add','sub','xor','and','or','mul','shl','lshr','ashr','rol','ror'))
            expr=BinOp(op,expr,Const(rng.randrange(256),8),8)
            if rng.randrange(4)==0:expr=UnOp(rng.choice(('not','neg')),expr,8)
        value,known=known_bits(expr)
        assert value&~known==0
        assert all(evaluate(expr,{'x':n})&known==value for n in range(256))


def test_cast_facts_and_budget():
    x=Var('x',8)
    for op in ('zx','sx'):
        e=UnOp(op,BinOp('or',x,Const(128,8),8),32)
        value,known=known_bits(e)
        assert all(evaluate(e,{'x':n})&known==value for n in range(256))
    def stop():raise ValueError('work limit')
    with pytest.raises(ValueError,match='work limit'):known_bits(x,tick=stop)
