from dataclasses import replace

import pytest

from binja_d810.deadline import Deadline
from binja_d810.ir.expr import BinOp, Const, Var
from binja_d810.semantic.verify import Verdict, check_equivalent
from binja_d810.value_sets.closure import Limits, Update, closure, embed_update
from binja_d810.value_sets.proof import check


def test_xor_updates_form_a_closed_set_including_every_guard_order():
    found = closure(0x1234, 16, [Update("xor", 0x10), Update("xor", 0x400)])
    assert found.values == {0x1234, 0x1224, 0x1634, 0x1624}
    for value in found.values:
        assert value ^ 0x10 in found.values and value ^ 0x400 in found.values


def test_addition_wraps_at_the_actual_width_and_large_cycles_are_rejected():
    assert closure(0xfe, 8, [Update("add", 0x80)]).values == {0xfe, 0x7e}
    found = closure(0xfe, 8, [Update("add", 1)])
    assert not found.values and found.reason == "mutable value set limit"


@pytest.mark.parametrize("limit", ("values", "writes", "steps", "bits"))
def test_each_closure_limit_leaves_no_partial_set(limit):
    found = closure(0, 32, [Update("xor", 1), Update("xor", 2)], limits=replace(Limits(), **{limit: 1}))
    assert not found.values and found.reason


def test_unknown_writes_and_expired_work_cannot_authorize_a_domain():
    assert closure(None, 32, []).reason
    assert closure(0, 32, [object()]).reason
    assert closure(0, 32, [Update("or", 1)]).reason
    assert closure(0, 32, [], deadline=Deadline.after(0)).reason


def test_only_expressions_invariant_across_the_entire_set_fold():
    key = Var("load_1", 16)
    domain = closure(0x1234, 16, [Update("xor", 0x40)]).values
    masked = BinOp("and", key, Const(0x3f, 16), 16)
    assert check(masked, Const(0x34, 16), {key.name: domain}).verdict is Verdict.PROVEN
    assert check(key, Const(0x1234, 16), {key.name: domain}).verdict is Verdict.REFUTED
    assert check_equivalent(masked, Const(0x34, 16)) is Verdict.REFUTED  # no constrained-cache leakage


def test_two_reads_of_one_mutable_global_are_independent_observations():
    first, second = Var("read_1", 32), Var("read_2", 32)
    expression = BinOp("xor", first, second, 32)
    assert check(expression, Const(0, 32), {"read_1": {1, 2}, "read_2": {1, 2}}).verdict is Verdict.REFUTED


def test_empty_unknown_or_wrong_width_domains_never_prove_vacuously():
    value = Var("x", 8)
    for domains in ({"x": set()}, {"x": {256}}, {"x": {-1}}, {"unknown": {1}}):
        assert check(value, Const(1, 8), domains).verdict is Verdict.TESTED
    assert check(value, Const(1, 8), {"x": {1}}, deadline=Deadline.after(0)).verdict is Verdict.TESTED


@pytest.mark.parametrize('little', (False, True))
def test_partial_xor_updates_preserve_every_other_byte(little):
    order = 'little' if little else 'big'
    for total in (2, 4, 8):
        original = bytes(range(16, 16 + total))
        for offset in range(total):
            for size in range(1, total - offset + 1):
                constant = (1 << (8 * size)) - 17
                update = embed_update(Update('xor', constant), offset, size, total, little_endian=little)
                expected = bytearray(original)
                changed = int.from_bytes(expected[offset:offset+size], order) ^ constant
                expected[offset:offset+size] = changed.to_bytes(size, order)
                assert int.from_bytes(original, order) ^ update.constant == int.from_bytes(expected, order)
    with pytest.raises(ValueError): embed_update(Update('add', 1), 1, 1, 4, little_endian=little)


def test_random_small_closures_match_an_exhaustive_independent_fixedpoint():
    import random
    for seed in range(64):
        rng = random.Random(seed)
        width = rng.randrange(2, 9); mask = (1 << width) - 1
        initial, a, b = (rng.randrange(mask + 1) for _ in range(3))
        expected = {initial}
        while True:
            expanded = expected | {v ^ a for v in expected} | {(v + b) & mask for v in expected}
            if expanded == expected: break
            expected = expanded
        result = closure(initial, width, [Update('xor', a), Update('add', b)])
        if len(expected) <= Limits().values: assert result.values == expected and not result.reason
        else: assert not result.values and result.reason == 'mutable value set limit'
