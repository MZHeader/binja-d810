"""Path simulation and facts (v4.2, spec §3.1 and §3.3)."""

from toygraph import toy

from binja_d810.ir.expr import BinOp, Const, Var
from binja_d810.unflatten.graph import Branch, dominators
from binja_d810.unflatten.thread import MAX_STEPS, facts, simulate
from binja_d810.unflatten.values import Values

A, B, C = 0x2c4e1101, 0x2c4e2202, 0x2c4e3303


def run(g, pred, start, env=None):
    return simulate(g, Values(g), pred, start, env or {})


def test_a_known_state_runs_through_a_compare_tree():
    g = toy({0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
             1: {"phi": {"s#2": {0: "s#1"}}, "if": (f"s#2 == {C:#x}", 7, 2)},
             2: {"if": (f"s#2 == {B:#x}", 8, 3)},
             3: {"if": (f"s#2 == {A:#x}", 9, 1)},
             7: {"stop": True}, 8: {"stop": True}, 9: {"stop": True}})
    out = run(g, 0, 1)
    assert out.path == (1, 2, 3) and out.dest == 9 and out.decided == (1, 2, 3)


def test_a_phi_entered_again_drops_an_unknown_source():
    g = toy({0: {"do": ["t#1 = 5"], "goto": 1},
             1: {"phi": {"t#2": {0: "t#1", 2: "t#3"}}, "if": ("t#2 == 5", 2, 3)},
             2: {"do": ["t#3 = ?"], "goto": 1},
             3: {"stop": True}})
    out = run(g, 0, 1)
    assert out.path == (1, 2) and out.dest == 1 and out.decided == (1,)


def test_an_assignment_executed_again_drops_its_old_value():
    g = toy({0: {"do": ["k#1 = 1"], "goto": 1},
             1: {"phi": {"k#2": {0: "k#1", 2: "k#3"}}, "do": ["u#1 = k#2 + 1"], "if": ("u#1 == 2", 2, 3)},
             2: {"do": ["k#3 = ?"], "goto": 1},
             3: {"stop": True}})
    out = run(g, 0, 1)
    assert out.dest == 1 and out.decided == (1,)


def test_phis_read_their_sources_together():
    g = toy({0: {"do": ["a#1 = 1", "b#1 = 2"], "goto": 1},
             1: {"phi": {"a#2": {0: "a#1", 1: "b#2"}, "b#2": {0: "b#1", 1: "a#2"}},
                 "if": ("a#2 == 1", 1, 2)},
             2: {"if": ("b#2 == 1", 3, 4)}, 3: {"stop": True}, 4: {"stop": True}})
    out = run(g, 0, 1)  # swap: (1, 2) -> (2, 1)
    assert out.path == (1, 1, 2) and out.dest == 3


def test_an_impure_step_is_kept_and_its_writes_become_unknown():
    g = toy({0: {"goto": 1},
             1: {"do": ["s#1 = 0x10", "t#1 = ?", "!"], "if": ("s#1 == 0x10", 2, 3)},
             2: {"if": ("t#1 == 0", 3, 4)}, 3: {"stop": True}, 4: {"stop": True}})
    out = run(g, 0, 1, {g.key("t#1"): 0})
    assert out.path == (1,) and out.dest == 2 and out.decided == (1,)


def _table(state, memory):
    return toy({0: {"do": [f"s#1 = {state}"], "goto": 1},
                1: {"phi": {"s#2": {0: "s#1"}},
                    "jump": ("L", {"L": ("0x1000 + s#2 * 4", 32)}, {0x2000: 2, 0x2010: 3, 0x2020: 4})},
                2: {"stop": True}, 3: {"stop": True}, 4: {"stop": True}}, memory=memory)


TABLE = {0x1000: 0x2000, 0x1004: 0x2010, 0x1008: 0x2020}


def test_a_jump_table_is_decided_by_constant_memory():
    out = run(_table(2, TABLE), 0, 1)
    assert out.dest == 4 and out.decided == (1,)


def test_a_jump_outside_constant_memory_is_undecided():
    out = run(_table(5, TABLE), 0, 1)
    assert out.dest == 1 and out.decided == ()


def test_a_jump_to_an_unknown_target_is_undecided():
    out = run(_table(3, {**TABLE, 0x100c: 0x9999}), 0, 1)
    assert out.dest == 1 and out.decided == ()


def test_the_path_stops_at_an_unknown_condition_and_at_a_stop():
    g = toy({0: {"goto": 1}, 1: {"if": ("x#1 == 0", 2, 3)}, 2: {"stop": True}, 3: {"goto": 2}})
    assert run(g, 0, 1).dest == 1
    assert run(g, 1, 3) .path == (3,) and run(g, 1, 3).dest == 2


def test_the_path_is_capped():
    g = toy({0: {"goto": 1}, 1: {"goto": 1}})
    assert len(run(g, 0, 1).path) == MAX_STEPS


def _fact(loop: bool):
    # 1 tests v#2; its true successor 2 dominates 3. v#2 is written in 4, which reaches 3 again
    # (without retesting) only in the loop version.
    return toy({0: {"goto": 1},
                1: {"if": ("v#2 == 0x77", 2, 5)},
                2: {"goto": 3},
                3: {"if": ("y#1 == 0", 4, 6)},
                4: {"do": ["v#2 = ?"], "goto": 3 if loop else 6},
                5: {"stop": True}, 6: {"stop": True}})


def test_a_dominating_test_is_a_fact():
    g = _fact(loop=False)
    assert facts(g, Values(g), dominators(g), 3) == {g.key("v#2"): 0x77}


def test_a_test_whose_value_is_written_again_is_no_fact():
    g = _fact(loop=True)
    assert facts(g, Values(g), dominators(g), 3) == {}


def test_a_false_edge_of_not_equal_is_a_fact():
    g = toy({0: {"goto": 1}, 1: {"if": ("v#1 != 0x77", 5, 2)}, 2: {"goto": 3}, 3: {"stop": True},
             5: {"stop": True}})
    assert facts(g, Values(g), dominators(g), 3) == {g.key("v#1"): 0x77}


def test_a_narrow_test_is_no_fact():
    g = toy({0: {"goto": 1}, 1: {"if": ("v#1 == 0x77", 2, 5)}, 2: {"goto": 3}, 3: {"stop": True},
             5: {"stop": True}})
    low = Var("v#1", 8, ref=g.key("v#1"))  # the low byte of a 32-bit value
    g._terms[1] = Branch(BinOp("eq", low, Const(0x77, 8), 1), 2, 5)
    assert facts(g, Values(g), dominators(g), 3) == {}
