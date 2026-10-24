import itertools,random
import pytest
from binja_d810.ir.expr import Var,Const,BinOp,UnOp
from binja_d810.ir.evaluate import evaluate
from binja_d810.semantic.finite_preimage import Limits,recover


def chain(width,bits,seed=0):
    rng=random.Random(seed)
    state=Const(rng.getrandbits(width),width)
    chunks=[BinOp('and',Var(n,width),Const((1<<bits)-1,width),width) for n in ('a','b','c','d')]
    for value in chunks:
        state=BinOp('xor',state,value,width)
        state=BinOp('mul',state,Const(rng.getrandbits(width)|1,width),width)
        state=BinOp('rol',state,Const(rng.randrange(width),width),width)
        state=BinOp('add',state,Const(rng.getrandbits(width),width),width)
    return state


@pytest.mark.parametrize('width',(8,16,32,64))
def test_complete_preimages_against_independent_exhaustive_inputs(width):
    for seed in range(4):
        expression=chain(width,2,seed)
        target=evaluate(expression,dict(a=1,b=2,c=3,d=0))
        result=recover(expression,target,limits=Limits(states=256,solutions=256))
        assert result is not None
        for values in itertools.product(range(4),repeat=4):
            env=dict(zip(('a','b','c','d'),values))
            assert evaluate(result.expression,env)==int(evaluate(expression,env)==target)


def test_correlated_chunk_expressions_keep_their_constraints():
    x=Var('x',8)
    lo=BinOp('and',x,Const(3,8),8)
    hi=BinOp('and',BinOp('lshr',x,Const(2,8),8),Const(3,8),8)
    expression=BinOp('mul',BinOp('xor',BinOp('mul',BinOp('xor',Const(19,8),lo,8),Const(13,8),8),hi,8),Const(21,8),8)
    for target in range(256):
        result=recover(expression,target,limits=Limits(states=256,solutions=256))
        assert result is not None
        for value in range(256):
            assert evaluate(result.expression,{'x':value})==int(evaluate(expression,{'x':value})==target)


def test_incomplete_searches_never_return_partial_solutions():
    expression=chain(16,4)
    with pytest.raises(ValueError,match='state limit'):recover(expression,1,limits=Limits(states=8))
    def stop():raise ValueError('deadline')
    with pytest.raises(ValueError,match='deadline'):recover(expression,1,tick=stop)
    x=Var('x',32)
    with pytest.raises(ValueError,match='seed domain'):
        recover(BinOp('mul',x,Const(2,32),32),0)


def test_repeated_side_inputs_cannot_be_assigned_conflicting_values():
    x=Var('x',8);digit=BinOp('and',x,Const(3,8),8)
    state=Const(31,8)
    for m in (7,13,19,23):state=BinOp('mul',BinOp('xor',state,digit,8),Const(m,8),8)
    for target in range(256):
        result=recover(state,target,limits=Limits(states=256,solutions=256))
        assert result is not None
        assert all(evaluate(result.expression,{'x':v})==int(evaluate(state,{'x':v})==target) for v in range(256))


def test_known_bit_constants_need_no_enumeration():
    from binja_d810.ir.expr import UnOp
    x=Var('x',32)
    expression=BinOp('and',UnOp('zx',x,64),Const(0xffffffff00000000,64),64)
    assert recover(expression,0).expression==Const(1,1)
    assert recover(expression,1).expression==Const(0,1)


def test_variable_rotations_reverse_subtraction_and_unary_stages():
    x=Var('x',8)
    digit=BinOp('and',x,Const(3,8),8)
    count=BinOp('and',BinOp('lshr',x,Const(2,8),8),Const(3,8),8)
    state=BinOp('sub',digit,Const(197,8),8)
    state=UnOp('not',UnOp('neg',state,8),8)
    state=BinOp('ror',state,count,8)
    for target in range(256):
        result=recover(state,target,limits=Limits(states=256,solutions=256))
        assert result is not None
        for value in range(256):
            assert evaluate(result.expression,{'x':value})==int(evaluate(state,{'x':value})==target)


def test_solution_cap_rejects_the_complete_result():
    a=BinOp('and',Var('a',8),Const(3,8),8)
    b=BinOp('and',Var('b',8),Const(3,8),8)
    with pytest.raises(ValueError,match='solution limit'):
        recover(BinOp('add',a,b,8),3,limits=Limits(states=256,solutions=1))
