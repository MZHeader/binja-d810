import pytest
from binja_d810.flags.liveness import live_after


def test_only_the_last_reaching_write_needs_a_source_model():
    edges = {i: (i+1,) for i in range(1000)}; edges[1000] = ()
    live = live_after(edges, {1000: {'v'}}, {i: {'v'} for i in range(1000)})
    assert live[999] == {'v'} and all(not live[i] for i in range(999))


def test_branches_and_loop_paths_preserve_all_possible_flag_consumers():
    edges = {0:(1,2), 1:(3,), 2:(3,), 3:(4,), 4:(0,5), 5:()}
    live = live_after(edges, {1:{'n'}, 3:{'v'}}, {0:{'n','v'}, 2:{'v'}})
    assert live[0] == {'n','v'}
    assert live[2] == {'v'}
    assert not live[4]  # The loop head overwrites both flags before the next read.


def test_flag_liveness_obeys_the_shared_work_budget():
    def expired(): raise ValueError('shared budget')
    with pytest.raises(ValueError, match='shared budget'):
        live_after({0:(1,),1:()}, {1:{'v'}}, {}, expired)
