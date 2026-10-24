"""All-path byte provenance and conservative joins, independent of Binary Ninja."""
import random
import pytest

from binja_d810.ir.expr import BinOp, Const, Var
from binja_d810.summaries.control import TrackedFrame, join, order
from binja_d810.summaries.state import Register, State


def inputs():
    state = State({'sp': 64, 'x': 32})
    frame = TrackedFrame(32)
    frame.stack_position = lambda: -32
    return state, frame


def test_many_diamonds_are_visited_once_without_enumerating_paths():
    edges = {}
    for i in range(100):
        n = i * 3
        edges[n], edges[n+1], edges[n+2] = (n+1,n+2), (n+3,), (n+3,)
    edges[300] = ()
    steps = 0
    def tick():
        nonlocal steps
        steps += 1
        assert steps < 2000  # 2**100 paths, but only 301 blocks and 400 edges.
    ordered, preds = order(edges, 0, tick)
    assert len(ordered) == len(set(ordered)) == 301
    assert preds[3] == {1,2}


@pytest.mark.parametrize('edges', ({0:(1,),1:(0,)}, {0:(0,)}, {0:(2,)}))
def test_loops_and_missing_targets_are_not_proved(edges):
    with pytest.raises(ValueError): order(edges, 0, lambda: None)


def test_unreachable_blocks_do_not_fabricate_a_path_to_a_call():
    ordered, preds = order({0:(1,),1:(),2:(2,)}, 0, lambda: None)
    assert ordered == [0,1] and set(preds) == {0,1}


def test_unknown_scalars_are_independent_but_equal_values_stay_exact():
    a, fa = inputs(); b, fb = a.copy(), fa.copy()
    a.write(Register('x',32), Const(1,32)); b.write(Register('x',32), Const(2,32))
    fa.write(-4,Const(17,32)); fb.write(-4,Const(17,32))
    state, frame = join([(a,fa),(b,fb)], 3, 'sp', lambda *args: None)
    assert isinstance(state.values['x'],Var) and state.values['x'].name != 'x'
    assert state.values['sp'] == Var('sp',64)
    assert frame.read(-4,4) == Const(17,32)
    state.write(Register('x',32),Const(3,32)); frame.write(-4,Const(9,32))
    assert a.values['x'] == Const(1,32) and b.values['x'] == Const(2,32)
    assert fa.read(-4,4) == fb.read(-4,4) == Const(17,32)


@pytest.mark.parametrize('place', ('register', 'byte'))
def test_merges_never_hide_a_possible_stack_address(place):
    a, fa = inputs(); b, fb = a.copy(), fa.copy()
    derived = BinOp('add',Var('sp',64),Const(-8,64),64)
    if place == 'register':
        a.values['sp'] = derived
    else:
        fa.write(-16,derived); fb.write(-16,Const(0,64))
    with pytest.raises(ValueError,match='frame-derived'):
        join([(a,fa),(b,fb)], 3, 'sp', lambda *args: None)


@pytest.mark.parametrize('overwrite', (0,1,4))
def test_any_path_read_preserves_a_writer_until_every_byte_is_overwritten(overwrite):
    state, frame = inputs()
    frame.owner = 7; frame.write(-4,Const(0x12345678,32)); frame.owner = None
    a,b = frame.copy(),frame.copy()
    a.write(-4,Const(0,32))
    if overwrite: b.write(-4,Const(0,overwrite*8))
    _, merged = join([(state,a),(state,b)], 3, 'sp', lambda *args: None)
    merged.read(-4,4)
    assert (7 in frame.effects.live) == (overwrite != 4)


def test_distinct_call_sites_keep_both_possible_byte_writers():
    state, a = inputs(); b = a.copy()
    for frame, owner in ((a,7),(b,9)):
        frame.owner = owner; frame.write(-4,Const(owner,32)); frame.owner = None
    _, merged = join([(state,a),(state,b)], 3, 'sp', lambda *args: None)
    merged.read(-4,1)
    assert merged.effects.live == {7,9}


def test_same_call_round_trip_does_not_hide_an_earlier_call_read():
    _, frame = inputs()
    frame.owner = 7; frame.write(-4,Const(7,32)); frame.read(-4,4)
    assert not frame.effects.live
    frame.owner = 9; frame.read(-4,4)
    assert frame.effects.live == {7}


def test_join_obeys_the_frame_union_and_work_limits():
    state, a = inputs(); b = a.copy()
    a.write(-32,Const(0,256)); b.write(0,Const(0,8))
    with pytest.raises(ValueError,match='frame byte limit'):
        join([(state,a),(state,b)],3,'sp',lambda *args: None)
    def expired(*args): raise ValueError('work limit')
    with pytest.raises(ValueError,match='work limit'):
        join([(state,a)],3,'sp',expired)


def test_random_dags_match_an_independent_enumeration_of_byte_writers():
    rng = random.Random(810)
    for _ in range(60):
        edges = {n: tuple(sorted({n+1,rng.randrange(n+1,8)})) for n in range(7)}
        edges[7] = ()
        actions = {}
        for n in edges:
            size = rng.choice((1,2,4)); offset = rng.randrange(9-size)-8
            actions[n] = rng.choice(('read','caller','helper')),offset,size
        state, initial = inputs()
        initial.owner = 7; initial.write(-8,Const(0,64)); initial.owner = None
        ordered,preds = order(edges,0,lambda:None)
        states = {}
        for n in ordered:
            s,f = join([states[p] for p in sorted(preds[n])] if n else [(state,initial)],n,'sp',lambda *a:None)
            op,offset,size = actions[n]
            if op == 'read': f.read(offset,size)
            else:
                f.owner = n+100 if op == 'helper' else None
                f.write(offset,Const(n,size*8)); f.owner = None
            states[n] = s,f
        observed = set()
        def enumerate_paths(n,writers):
            writers = writers.copy()
            op,offset,size = actions[n]
            for at in range(offset,offset+size):
                if op == 'read':
                    if writers[at] is not None: observed.add(writers[at])
                else: writers[at] = n+100 if op == 'helper' else None
            for target in edges[n]: enumerate_paths(target,writers)
        enumerate_paths(0,{at:7 for at in range(-8,0)})
        assert initial.effects.live == observed
