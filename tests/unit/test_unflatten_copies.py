"""Ordering a parallel copy (v4.3 spec 3.3): a self tail call's parameters assigned on its back edge."""

from binja_d810.unflatten.copies import sequence


def test_independent_moves_keep_their_order():
    assert sequence([("a", frozenset()), ("b", frozenset({"x"})), ("c", frozenset({"y"}))]) == [0, 1, 2]


def test_a_move_whose_destination_another_reads_waits():
    # a <- b, b <- c: writing b first would lose the b that a reads.
    assert sequence([("b", frozenset({"c"})), ("a", frozenset({"b"}))]) == [1, 0]


def test_a_move_may_read_its_own_destination():
    assert sequence([("a", frozenset({"a"})), ("b", frozenset({"a"}))]) == [1, 0]


def test_a_cycle_has_no_order():
    assert sequence([("a", frozenset({"b"})), ("b", frozenset({"a"}))]) is None


def test_a_chain_is_ordered_from_its_end():
    moves = [("a", frozenset({"b"})), ("b", frozenset({"c"})), ("c", frozenset({"d"}))]
    assert sequence(moves) == [0, 1, 2]
    assert sequence(list(reversed(moves))) == [2, 1, 0]
