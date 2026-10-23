import json
import random

import pytest

from binja_d810.allocations.contracts import Contract, parse
from binja_d810.allocations.lifetime import Event, prove
from binja_d810.deadline import Deadline
from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import Const, Var


A = Contract("allocate", 0, True, True)
F = Contract("deallocate", 0, False, True)


def plan(events, allocation=A, release=F, **kwargs):
    return prove(events, 8, allocation, release, max_bytes=8, max_events=100, **kwargs)


def test_full_elision_needs_explicit_failure_and_effect_guarantees():
    events = [Event("store", 0, 0, 4, Var("value", 32)), Event("load", 1, 0, 4), Event("free", 2)]
    assert plan(events).elide
    for alloc, free in ((Contract("allocate", 0, True, False), F),
                        (A, Contract("deallocate", 0))):
        found = plan(events, alloc, free)
        assert found.loads == {1: Var("value", 32)} and not found.elide and found.notes
    nullable = plan(events, Contract("allocate", 0))
    assert not nullable.loads and not nullable.elide and nullable.notes


@pytest.mark.parametrize("little_endian", (False, True))
def test_random_partial_stores_match_an_independent_bytearray(little_endian):
    rng = random.Random(4321)
    order = "little" if little_endian else "big"
    for _ in range(100):
        data = bytearray(8)
        events = [Event("store", 0, 0, 8, Const(0, 64))]
        for step in range(12):
            size = rng.choice((1, 2, 4, 8))
            offset = rng.randrange(9 - size)
            value = rng.getrandbits(size * 8)
            data[offset:offset + size] = value.to_bytes(size, order)
            events.append(Event("store", step + 1, offset, size, Const(value, size * 8)))
        events += [Event("load", "result", 0, 8), Event("free", "free")]
        found = plan(events, little_endian=little_endian)
        assert found.elide and evaluate(found.loads["result"], {}) == int.from_bytes(data, order)


def test_unknown_bytes_prevent_elision_and_are_never_invented():
    events = [Event("store", 0, 0, 1, Const(17, 8)), Event("load", 1, 0, 4), Event("free", 2)]
    found = plan(events)
    assert not found.loads and not found.elide and found.notes


@pytest.mark.parametrize("event", (Event("escape", 2), Event("call", 2), Event("branch", 2),
                                  Event("store", 2, -1, 4, Const(0, 32)), Event("load", 2, 7, 4)))
def test_eventual_escapes_unknown_effects_and_out_of_bounds_accesses_cancel_the_proof(event):
    found = plan([Event("store", 0, 0, 4, Const(9, 32)), Event("load", 1, 0, 4), event])
    assert not found.loads and not found.elide and found.notes


def test_release_order_and_budget_are_checked():
    assert not plan([Event("free", 0), Event("load", 1, 0, 4)]).elide
    assert not plan([Event("free", 0), Event("free", 1)]).elide
    assert not plan([Event("free", 0)], deadline=Deadline.after(0)).elide
    assert not plan([Event("store", 0, 0, 4, Const(0, 32)), Event("load", 0, 0, 4)]).loads


def test_metadata_is_explicit_and_not_inferred_from_a_function_name_or_purity():
    text = json.dumps(dict(version=1, role="allocate", parameter=0, fresh=True, ordinary=True))
    assert parse(text, 4096) == Contract("allocate", 0)
    for extra in ({"nonnull": 1}, {"pure": True}, {"name": "malloc"}, {"parameter": -1},
                  {"fresh": False}, {"ordinary": False}, {"version": 2}):
        spec = json.loads(text) | extra
        with pytest.raises(ValueError):
            parse(json.dumps(spec), 4096)
    with pytest.raises(ValueError):
        parse(text, 1)
