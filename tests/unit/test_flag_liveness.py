import pytest
import random
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


def test_reverse_layout_does_not_spend_the_budget_rescanning_the_whole_graph():
    edges = {i:(i-1,) for i in range(1,501)}; edges[0] = ()
    visits = 0
    def tick():
        nonlocal visits
        visits += 1
        assert visits <= 6000
    live = live_after(edges,{0:{'v'}},{},tick)
    assert all(live[i] == {'v'} for i in range(1,501))


def test_worklist_matches_independent_path_search_on_cyclic_graphs():
    rng=random.Random(810)
    for _ in range(100):
        edges={i:tuple({rng.randrange(12) for _ in range(rng.randrange(3))}) for i in range(12)}
        reads={i:{f for f in ('n','z','c','v') if rng.randrange(5)==0} for i in edges}
        writes={i:{f for f in ('n','z','c','v') if rng.randrange(4)==0} for i in edges}
        actual=live_after(edges,reads,writes)
        def reaches_read(start,flag):
            pending=list(edges[start]);seen=set()
            while pending:
                node=pending.pop()
                if node in seen:continue
                seen.add(node)
                if flag in reads[node]:return True
                if flag not in writes[node]:pending.extend(edges[node])
            return False
        assert actual=={i:{f for f in ('n','z','c','v') if reaches_read(i,f)} for i in edges}
