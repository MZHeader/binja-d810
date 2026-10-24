import pytest
import random
from binja_d810.flags.uses import Confinement,Use


def proof(graph,allowed=()):
    return Confinement(lambda value:graph.get(value,()),allowed)


def test_dead_phi_chains_do_not_invent_an_observable_flag_use():
    graph={'flag':[Use(1),Use(2,'join')],'join':[Use(3,'exit')],'exit':[]}
    assert proof(graph,{1}).external('flag') is None


def test_phi_cycles_with_no_observer_are_safe():
    graph={'flag':[Use(1,'a')],'a':[Use(2,'b')],'b':[Use(3,'a')]}
    assert proof(graph).external('flag') is None


@pytest.mark.parametrize('description',('store','call','return','arithmetic'))
def test_any_real_consumer_after_a_phi_blocks_the_rewrite(description):
    graph={'flag':[Use(1),Use(2,'a')],'a':[Use(3,'b')],'b':[Use(4,'a'),Use(5,description=description)]}
    found=proof(graph,{1}).external('flag')
    assert found[1].index==5 and found[1].description==description


def test_failed_cycle_search_cannot_poison_the_success_cache():
    graph={'a':[Use(1,'b'),Use(2,description='store')],'b':[Use(3,'a')],'c':[Use(4,'b')]}
    check=proof(graph)
    assert all(check.external(v) is not None for v in ('a','b','c'))


def test_an_allowed_phi_still_checks_its_output_users():
    check=proof({'flag':[Use(1,'join')],'join':[Use(2)]},{1})
    assert check.external('flag')[1].index==2


def test_phi_reachability_obeys_the_existing_work_budget():
    def expired():raise ValueError('work limit')
    with pytest.raises(ValueError,match='work limit'):
        Confinement(lambda v:[Use(1,v)],set(),expired).external('flag')


def test_cached_queries_match_a_backward_observer_fixpoint():
    rng=random.Random(813)
    for _ in range(100):
        graph={v:[Use(100+rng.randrange(10),rng.randrange(10) if rng.randrange(2) else None)
                  for _ in range(rng.randrange(4))] for v in range(10)}
        allowed={100+i for i in range(10) if rng.randrange(2)}
        bad={v for v,uses in graph.items() if any(u.forwarded is None and u.index not in allowed for u in uses)}
        while True:
            more={v for v,uses in graph.items() if any(u.forwarded in bad for u in uses if u.forwarded is not None)}-bad
            if not more:break
            bad|=more
        check=proof(graph,allowed);order=list(graph);rng.shuffle(order)
        assert {v for v in order if check.external(v) is not None}==bad
