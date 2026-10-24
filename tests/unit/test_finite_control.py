import pytest
import random
from binja_d810.control.equivalence import Assignment
from binja_d810.control.finite import Loop,Decision,Break,Continue,terminates
from binja_d810.ir.expr import Var,Const,BinOp
from binja_d810.ir.parse import parse


def prove(loop,initial,limit=64):
    return terminates(loop,initial,iterations=limit,max_nodes=200,tick=lambda:None)


def condition(name,value,width,op='ne'):
    return BinOp(op,Var(name,width),Const(value,width),1)


@pytest.mark.parametrize('width',(8,16,32,64))
def test_unknown_arithmetic_does_not_hide_a_constant_trip_count(width):
    i,a=Var('i',width),Var('a',width)
    body=(Assignment(a,parse('(a ^ x) * 7',width)),Decision(condition('i',7,width,'eq'),(Break(),)),
          Assignment(i,parse('i + 1',width)))
    assert prove(Loop(Const(1,1),body),{'i':0,'a':3})==8


def test_wraparound_is_evaluated_at_the_original_width():
    i=Var('i',8)
    loop=Loop(condition('i',1,8),(Assignment(i,parse('i + 1',8)),))
    assert prove(loop,{'i':254})==3


def test_unknown_guards_and_nontermination_are_rejected():
    with pytest.raises(ValueError,match='unknown'):
        prove(Loop(condition('x',0,32),()),{})
    with pytest.raises(ValueError,match='iteration limit'):
        prove(Loop(Const(1,1),()),{})


def test_continue_and_post_test_loops():
    i=Var('i',8)
    inc=Assignment(i,parse('i + 1',8))
    assert prove(Loop(condition('i',3,8),(Continue(),),update=(inc,)),{'i':0})==3
    assert prove(Loop(Const(0,1),(inc,),post_test=True),{'i':0})==1
    with pytest.raises(ValueError,match='iteration limit'):
        prove(Loop(condition('i',3,8),(Continue(),inc)),{'i':0})


def test_exact_bound_and_zero_iterations():
    i=Var('i',8)
    loop=Loop(condition('i',4,8),(Assignment(i,parse('i + 1',8)),))
    assert prove(loop,{'i':0},4)==4
    assert prove(loop,{'i':4},0)==0
    with pytest.raises(ValueError,match='iteration limit'):prove(loop,{'i':0},3)


def test_budget_failure_never_certifies_a_loop():
    def stop():raise ValueError('deadline')
    with pytest.raises(ValueError,match='deadline'):
        terminates(Loop(Const(1,1),(Break(),)),{},iterations=4,max_nodes=200,tick=stop)


@pytest.mark.parametrize('width',(8,16,32,64))
def test_random_steps_and_starts_match_an_independent_counter(width):
    rng=random.Random(width+431)
    modulus=1<<width
    i,a=Var('i',width),Var('a',width)
    for _ in range(50):
        start=rng.randrange(modulus);step=rng.randrange(1,modulus,2);count=rng.randrange(1,24)
        end=(start+step*count)%modulus
        value=start;expected=0
        while value!=end:
            value=(value+step)%modulus;expected+=1
            assert expected<=24
        loop=Loop(condition('i',end,width),
                  (Assignment(a,parse('(a ^ x) * 13',width)),Assignment(i,BinOp('add',i,Const(step,width),width))))
        assert prove(loop,{'i':start})==expected


def test_signed_control_uses_the_operand_width():
    i=Var('i',8)
    loop=Loop(condition('i',-100,8,'slt'),(Assignment(i,parse('i + 1',8)),))
    assert prove(loop,{'i':128})==28
