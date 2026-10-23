import pytest

from binja_d810.scratch.values import Use, unobserved


def check(graph, start, mask):
    return unobserved(start, mask, lambda value: graph.get(value, ()), lambda: None)


def test_an_unrelated_variable_write_cannot_kill_a_live_ssa_value():
    # These values may have occupied the same physical stack location. The old value's
    # logical use remains live independently of the other assignment.
    assert not check({"old": [Use(reads=15)], "other": []}, "old", 15)


def test_partial_redefinitions_carry_only_the_unchanged_bytes():
    graph = {1: [Use(follows=2, overwritten=1)], 2: [Use(reads=14)]}
    assert not check(graph, 1, 15)
    assert check(graph, 1, 1)
    graph[2] = [Use(reads=1)]
    assert check(graph, 1, 15)


def test_merges_and_cycles_preserve_every_possible_observation():
    graph = {1: [Use(follows=2), Use(follows=3)], 2: [Use(follows=1)], 3: [Use(reads=8)]}
    assert not check(graph, 1, 8)
    assert check(graph, 1, 7)


def test_unknown_uses_and_invalid_masks_cannot_prove_deadness():
    assert not check({1: [Use(unknown=True)]}, 1, 1)
    assert not check({1: [Use(follows=2, overwritten=-1)]}, 1, 1)


def test_work_budget_is_checked_even_through_a_phi_cycle():
    left = 2
    def tick():
        nonlocal left
        left -= 1
        if left < 0:
            raise ValueError("exhausted")
    with pytest.raises(ValueError, match="exhausted"):
        unobserved(1, 15, lambda _: [Use(follows=2)], tick)
