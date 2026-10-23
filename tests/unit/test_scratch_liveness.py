"""Compare the fixpoint with an independent exhaustive path search over remaining bytes."""

import random

from binja_d810.deadline import Deadline
from binja_d810.scratch.liveness import Access, analyze


def observed_after(store, accesses, successors, exit_live=0):
    work = [(next_node, accesses[store].writes) for next_node in successors[store]]
    if not work:
        return bool(accesses[store].writes & exit_live)
    seen = set()
    while work:
        node, remaining = work.pop()
        if not remaining or (node, remaining) in seen:
            continue
        seen.add((node, remaining))
        access = accesses[node]
        if access.reads & remaining:
            return True
        remaining &= ~access.writes
        if not successors[node] and remaining & exit_live:
            return True
        work.extend((target, remaining) for target in successors[node])
    return False


def test_a_full_overwrite_must_hold_on_every_path():
    accesses = {0: Access(writes=15, removable=True), 1: Access(writes=15), 2: Access(), 3: Access(reads=15)}
    edges = {0: (1, 2), 1: (3,), 2: (3,), 3: ()}
    assert 0 not in analyze(accesses, edges, max_visits=100).dead
    accesses[2] = Access(writes=15)
    assert 0 in analyze(accesses, edges, max_visits=100).dead


def test_partial_overwrites_do_not_hide_the_other_bytes():
    accesses = {0: Access(writes=15, removable=True), 1: Access(writes=1), 2: Access(reads=14)}
    edges = {0: (1,), 1: (2,), 2: ()}
    assert 0 not in analyze(accesses, edges, max_visits=100).dead
    accesses[2] = Access(reads=1)
    assert 0 in analyze(accesses, edges, max_visits=100).dead


def test_loop_backedges_keep_later_observations_live():
    accesses = {0: Access(writes=3, removable=True), 1: Access(), 2: Access(reads=2), 3: Access()}
    edges = {0: (1,), 1: (2, 3), 2: (1,), 3: ()}
    assert 0 not in analyze(accesses, edges, max_visits=100).dead


def test_a_dead_loop_write_can_go_without_changing_the_loop():
    accesses = {0: Access(writes=3, removable=True), 1: Access()}
    edges = {0: (1,), 1: (0,)}
    assert analyze(accesses, edges, max_visits=100).dead == {0}
    assert edges == {0: (1,), 1: (0,)}


def test_unknown_calls_are_explicit_read_barriers():
    accesses = {0: Access(writes=255, removable=True), 1: Access(reads=255)}
    assert not analyze(accesses, {0: (1,), 1: ()}, max_visits=100).dead


def test_effectful_store_values_are_not_removed():
    accesses = {0: Access(writes=255, removable=False)}
    assert not analyze(accesses, {0: ()}, max_visits=100).dead


def test_exhaustion_never_returns_partial_deadness_proofs():
    accesses = {0: Access(writes=1, removable=True), 1: Access(reads=1)}
    edges = {0: (1,), 1: ()}
    for result in (analyze(accesses, edges, max_visits=1),
                   analyze(accesses, edges, max_visits=100, deadline=Deadline.after(0))):
        assert result.reason and not result.dead and result.live_out is None


def test_missing_edges_and_negative_masks_reject():
    assert analyze({0: Access(writes=1)}, {0: (1,)}, max_visits=100).reason
    assert analyze({0: Access(reads=-1)}, {0: ()}, max_visits=100).reason


def test_random_control_flow_matches_exhaustive_paths():
    rng = random.Random(190713)
    for _ in range(400):
        count = rng.randrange(1, 13)
        accesses = {i: Access(rng.randrange(16), rng.randrange(16), bool(rng.randrange(2))) for i in range(count)}
        edges = {i: tuple(rng.sample(range(count), rng.randrange(min(3, count) + 1))) for i in range(count)}
        exit_live = rng.randrange(16)
        result = analyze(accesses, edges, max_visits=10000, exit_live=exit_live)
        expected = {i for i, a in accesses.items() if a.removable and a.writes
                    and not observed_after(i, accesses, edges, exit_live)}
        assert not result.reason and result.dead == expected
