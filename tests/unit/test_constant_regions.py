import pytest
from binja_d810.consteval.regions import reachable, reverse, validate
from binja_d810.consteval.state import State


def test_later_loop_region_has_one_entry_and_preserves_its_exit():
    edges = {0: (1,), 1: (2,), 2: (3,), 3: (4, 5), 4: (3,), 5: (6,), 6: ()}
    assert validate(edges, reverse(edges), 2, 5, {2, 3, 4}) == {5, 6}
    assert reachable(reverse(edges), 2) == {0, 1, 2}


def test_an_outside_branch_into_a_removed_interior_is_rejected():
    edges = {0: (1, 3), 1: (2,), 2: (3,), 3: (4,), 4: ()}
    with pytest.raises(ValueError, match='additional entry'):
        validate(edges, reverse(edges), 1, 4, {1, 2, 3})


def test_a_retained_outer_loop_cannot_reenter_the_region():
    edges = {0: (1,), 1: (2,), 2: (3,), 3: (1, 4), 4: ()}
    with pytest.raises(ValueError, match='reenter'):
        validate(edges, reverse(edges), 1, 3, {1, 2})


def test_unexecuted_constant_branch_arms_dominated_by_the_entry_are_safe():
    edges = {0: (1, 2), 1: (3,), 2: (3,), 3: (4,), 4: ()}
    assert validate(edges, reverse(edges), 0, 4, {0, 1, 3}) == {4}


def test_disjoint_branch_regions_can_each_keep_their_own_entry():
    edges = {0: (1, 3), 1: (2,), 2: (5,), 3: (4,), 4: (5,), 5: ()}
    assert validate(edges, reverse(edges), 1, 5, {1, 2}) == {5}
    assert validate(edges, reverse(edges), 3, 5, {3, 4}) == {5}


def test_region_proofs_charge_the_shared_budget():
    def expired(): raise ValueError('shared budget')
    with pytest.raises(ValueError, match='shared budget'):
        validate({0: (1,), 1: ()}, {0: (), 1: (0,)}, 0, 1, {0}, expired)


def test_plan_snapshots_are_independent_of_later_interpretation():
    before = State(4)
    before.store(0, 3, 1); before.assign('x', 7, 32)
    saved = before.copy()
    before.store(0, 9, 1); before.assign('x', 11, 32)
    assert saved.load(0, 1) == 3 and saved.read('x', 32) == 7 and saved.written == {0}
