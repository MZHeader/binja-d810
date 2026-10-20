"""Planning threads through dispatchers (v4.2, spec §3.1 and §3.4): splits, selection, caps and the plan."""

from toygraph import toy

from binja_d810.deadline import Deadline
from binja_d810.unflatten import thread
from binja_d810.unflatten.graph import dominators
from binja_d810.unflatten.thread import MAX_COPIED, MAX_DUP, Plan, candidates, plan, split_edge
from binja_d810.unflatten.values import Values

A, B, C, D = 0x5a5a1001, 0x5a5a2002, 0x5a5a3003, 0x5a5a4004
EMPTY = Plan((), frozenset(), frozenset())


def _tree(latch=()):
    """A compare tree over 4 sparse states: 1 tests A, 5 tests B, 6 tests C and 7 tests D. Handlers 2, 3 and 4
    set the next state and go to the latch 9; 8 returns."""
    return {
        0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 9: "s#9"}}, "if": (f"s#2 == {A:#x}", 2, 5)},
        5: {"if": (f"s#2 == {B:#x}", 3, 6)},
        6: {"if": (f"s#2 == {C:#x}", 4, 7)},
        7: {"if": (f"s#2 == {D:#x}", 8, 10)},
        2: {"do": ["!", f"s#3 = {B:#x}"], "goto": 9},
        3: {"do": ["!", f"s#4 = {C:#x}"], "goto": 9},
        4: {"do": ["!", f"s#5 = {D:#x}"], "goto": 9},
        8: {"do": ["!"], "stop": True},
        10: {"stop": True},
        9: {"phi": {"s#9": {2: "s#3", 3: "s#4", 4: "s#5"}}, "do": list(latch), "goto": 1},
    }


def _by_pred(p):
    return {t.pred: t for t in p.threads}


def _ends(p):
    return {(t.pred, t.outcome.dest) for t in p.threads}


# --- shapes ---

def test_a_compare_tree_threads_each_state_setting_edge():
    g = toy(_tree())
    p = plan(g)
    assert _ends(p) == {(0, 2), (2, 3), (3, 4), (4, 8)}
    assert p.dispatcher == {1, 5, 6, 7}
    assert p.states == {g.key("s#1").var}


def test_a_jump_table_dispatcher_is_evidenced_by_the_jump():
    g = toy({
        0: {"do": ["s#1 = 0"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 9: "s#9"}}, "if": ("s#2 u< 4", 11, 10)},
        11: {"jump": ("L", {"L": ("0x1000 + s#2 * 4", 32)}, {0x2000: 2, 0x2010: 3, 0x2020: 4, 0x2030: 8})},
        2: {"do": ["!", "s#3 = 1"], "goto": 9},
        3: {"do": ["!", "s#4 = 2"], "goto": 9},
        4: {"do": ["!", "s#5 = 3"], "goto": 9},
        8: {"do": ["!"], "stop": True},
        10: {"stop": True},
        9: {"phi": {"s#9": {2: "s#3", 3: "s#4", 4: "s#5"}}, "goto": 1},
    }, memory={0x1000 + 4 * i: 0x2000 + 0x10 * i for i in range(4)})
    p = plan(g)
    assert _ends(p) == {(0, 2), (2, 3), (3, 4), (4, 8)}
    assert p.dispatcher == {1, 11} and p.states == {g.key("s#1").var}


def test_nested_whiles_reach_the_next_handler():
    g = toy({
        0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 7: "s#6"}}, "goto": 2},
        2: {"phi": {"s#3": {1: "s#2", 3: "s#4"}}, "if": (f"s#3 == {A:#x}", 3, 4)},
        3: {"do": ["!", f"s#4 = {B:#x}"], "goto": 2},
        4: {"phi": {"s#5": {2: "s#3", 5: "s#7"}}, "if": (f"s#5 == {B:#x}", 5, 6)},
        5: {"do": ["!", f"s#7 = {C:#x}"], "goto": 4},
        6: {"if": (f"s#5 == {C:#x}", 7, 8)},
        7: {"do": ["!", f"s#6 = {D:#x}"], "goto": 1},
        8: {"stop": True},
    })
    p = plan(g)
    assert _ends(p) == {(0, 3), (3, 5), (5, 7), (7, 8)}
    assert p.dispatcher == {2, 4, 6}


def test_a_single_iteration_loop_is_resolved_with_the_tree_around_it():
    g = toy({
        0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 9: "s#9"}}, "if": (f"s#2 == {A:#x}", 2, 5)},
        5: {"if": (f"s#2 == {B:#x}", 3, 6)},
        6: {"if": (f"s#2 == {C:#x}", 4, 8)},
        2: {"goto": 11},
        11: {"phi": {"s#3": {2: "s#2", 12: "s#4"}}, "if": (f"s#3 != {A:#x}", 13, 12)},
        12: {"do": ["!", f"s#4 = {B:#x}"], "goto": 11},
        13: {"goto": 9},
        3: {"do": ["!", f"s#5 = {C:#x}"], "goto": 9},
        4: {"do": ["!", f"s#6 = {D:#x}"], "goto": 9},
        8: {"stop": True},
        9: {"phi": {"s#9": {13: "s#3", 3: "s#5", 4: "s#6"}}, "goto": 1},
    })
    p = plan(g)
    threads = _by_pred(p)
    assert threads[2].outcome.path == (11,) and threads[2].outcome.dest == 12  # the loop runs once
    assert threads[12].outcome.path == (11, 13, 9, 1, 5) and threads[12].outcome.dest == 3
    assert {1, 5, 6, 11} <= p.dispatcher


def test_a_one_boolean_state_splits_on_its_condition():
    g = toy({
        0: {"do": ["s#1 = 0x10"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 9: "s#9"}}, "if": ("s#2 == 0x10", 2, 5)},
        5: {"if": ("s#2 == 0x20", 3, 6)},
        6: {"if": ("s#2 == 0x30", 4, 8)},
        2: {"do": ["c#1 = ?", "s#3 = ((0 - zx32(c#1)) & 0x10) + 0x20"], "goto": 9},
        3: {"do": ["!", "s#4 = 0x30"], "goto": 9},
        4: {"do": ["!", "s#5 = 0x40"], "goto": 9},
        8: {"stop": True},
        9: {"phi": {"s#9": {2: "s#3", 3: "s#4", 4: "s#5"}}, "goto": 1},
    }, widths={"c": 1}, live={(2, "s#3"): "((0 - zx32(c#1)) & 0x10) + 0x20"})
    thread = _by_pred(plan(g))[2]
    split = thread.split
    assert thread.outcome is None and split.key == g.key("s#3")
    assert split.condition == g.expr("c#1") and (split.if_one, split.if_zero) == (0x30, 0x20)
    assert split.outcomes[0x30].dest == 4 and split.outcomes[0x20].dest == 3


def test_a_copy_of_a_phi_of_three_constants_splits_into_a_chain():
    spec = _tree()
    spec[2] = {"do": ["!"], "if": ("q#1 == 0", 15, 16)}
    spec.update({
        15: {"do": [f"s#10 = {B:#x}"], "goto": 12},
        16: {"if": ("q#1 == 1", 17, 18)},
        17: {"do": [f"s#11 = {C:#x}"], "goto": 12},
        18: {"do": [f"s#12 = {D:#x}"], "goto": 12},
        12: {"phi": {"s#13": {15: "s#10", 17: "s#11", 18: "s#12"}}, "goto": 13},
        13: {"do": ["!", "s#3 = s#13"], "goto": 9},
    })
    spec[9]["phi"] = {"s#9": {13: "s#3", 3: "s#4", 4: "s#5"}}
    split = _by_pred(plan(toy(spec)))[13].split
    assert split.condition is None and split.if_one is None and split.if_zero is None
    assert {v: o.dest for v, o in split.outcomes.items()} == {B: 3, C: 4, D: 8}


# --- rules ---

def test_no_split_on_a_phi_in_the_predecessor_itself():
    g = toy(_tree())
    values = Values(g)
    assert values.value_set(g.key("s#9")) == {B, C, D}  # a split would decide the tree
    assert split_edge(g, values, dominators(g), 9, 1) is None
    assert {2, 3, 4} <= {t.pred for t in plan(g).threads}


def test_no_split_on_a_copy_of_a_phi_in_the_predecessor():
    spec = _tree()  # the handler picks the next state with a branch; its merge copies the pick into the state
    spec[2] = {"do": ["!", f"t#1 = {B:#x}"], "if": ("q#1 == 0", 15, 16)}
    spec[15] = {"do": [f"t#2 = {C:#x}"], "goto": 16}
    spec[16] = {"phi": {"t#3": {2: "t#1", 15: "t#2"}}, "do": ["s#3 = t#3"], "goto": 9}
    spec[9]["phi"] = {"s#9": {16: "s#3", 3: "s#4", 4: "s#5"}}
    threads = _by_pred(plan(toy(spec)))
    assert 16 not in threads and threads[2].outcome.dest == 3 and threads[15].outcome.dest == 4


def test_a_fact_holds_for_a_copy_made_in_the_same_block():
    # At -O0 the dispatcher reloads the state (s#3 = s#2) and its tests are lifted through the copy, so the
    # fact is on s#2. A loop body that keeps the state passes the copy to the latch.
    g = toy({
        0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 9: "s#9"}}, "do": ["s#3 = s#2"], "if": (f"s#2 == {A:#x}", 2, 5)},
        5: {"if": (f"s#2 == {B:#x}", 3, 6)},
        6: {"if": (f"s#2 == {C:#x}", 4, 10)},
        2: {"do": ["!", f"s#4 = {B:#x}"], "goto": 9},
        3: {"if": ("q#1 u< 4", 7, 8)},
        7: {"do": ["!"], "goto": 9},
        8: {"do": [f"s#5 = {C:#x}"], "goto": 9},
        4: {"do": ["!"], "stop": True},
        10: {"stop": True},
        9: {"phi": {"s#9": {2: "s#4", 7: "s#3", 8: "s#5"}}, "goto": 1},
    })
    threads = _by_pred(plan(g, evidenced=frozenset({g.key("s#1").var})))
    assert threads[7].split is None and threads[7].outcome.dest == 3


def test_a_path_ends_at_the_first_handler_with_a_side_effect():
    threads = _by_pred(plan(toy(_tree())))
    assert threads[0].outcome.path == (1,) and threads[0].outcome.dest == 2
    assert threads[2].outcome.path == (9, 1, 5) and threads[2].outcome.dest == 3


def test_a_handler_without_side_effects_stays_on_the_path():
    spec = _tree()
    spec[3]["do"] = [f"s#4 = {C:#x}"]
    threads = _by_pred(plan(toy(spec)))
    assert threads[2].outcome.path == (9, 1, 5, 3, 9, 1, 5, 6) and threads[2].outcome.dest == 4


def test_at_most_eight_side_effects_are_copied_before_the_dispatcher():
    kept = plan(toy(_tree(latch=["!"] * MAX_DUP)))
    dropped = plan(toy(_tree(latch=["!"] * (MAX_DUP + 1))))
    assert {t.pred for t in kept.threads} == {0, 2, 3, 4}
    assert {t.pred for t in dropped.threads} == {0}


def test_at_most_sixty_four_instructions_are_copied():
    kept = plan(toy(_tree(latch=[f"t#{i} = {i:#x}" for i in range(1, MAX_COPIED + 1)])))
    dropped = plan(toy(_tree(latch=[f"t#{i} = {i:#x}" for i in range(1, MAX_COPIED + 2)])))
    assert {t.pred for t in kept.threads} == {0, 2, 3, 4}
    assert {t.pred for t in dropped.threads} == {0}


def test_a_path_that_never_leaves_the_dispatcher_is_dropped():
    spec = _tree()  # state C runs a handler without side effects that keeps state C
    spec[4] = {"do": [f"s#5 = {C:#x}"], "goto": 1}
    spec[1]["phi"] = {"s#2": {0: "s#1", 9: "s#9", 4: "s#5"}}
    spec[9]["phi"] = {"s#9": {2: "s#3", 3: "s#4"}}
    threads = _by_pred(plan(toy(spec)))
    assert set(threads) == {0, 2}  # 3 -> 9 is off the cycle: only the 64-block cap drops it


def test_a_path_back_to_its_own_start_is_dropped():
    spec = _tree()
    spec[2]["do"] = ["s#3 = s#2 + q#1"]  # no side effect, and the next state is unknown
    threads = _by_pred(plan(toy(spec)))
    assert 0 not in threads and {3, 4} <= set(threads)


def test_a_path_cut_at_its_own_start_is_kept():
    g = toy({  # handlers only set the state and an argument; the merge they share makes the call
        0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 8: "s#9"}}, "if": (f"s#2 == {A:#x}", 2, 5)},
        5: {"if": (f"s#2 == {B:#x}", 3, 6)},
        6: {"if": (f"s#2 == {C:#x}", 4, 7)},
        2: {"do": [f"s#3 = {B:#x}", "k#3 = 1"], "goto": 8},
        3: {"do": [f"s#4 = {C:#x}", "k#4 = 2"], "goto": 8},
        4: {"do": [f"s#5 = {D:#x}", "k#5 = 3"], "goto": 8},
        7: {"stop": True},
        8: {"phi": {"s#9": {2: "s#3", 3: "s#4", 4: "s#5"}}, "do": ["!"], "goto": 1},
    })
    threads = _by_pred(plan(g))
    assert threads[2].outcome.path == (8, 1, 5, 3) and threads[2].outcome.dest == 8
    assert threads[3].outcome.dest == 8 and threads[4].outcome.dest == 7


def test_a_small_machine_is_evidenced_by_the_compares_of_the_whole_function():
    # Three constants, but C is never assigned: the blocks threads decide compare only two.
    g = toy({
        0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 3: "s#3", 5: "s#2"}}, "if": (f"s#2 == {B:#x}", 4, 2)},
        2: {"if": (f"s#2 != {A:#x}", 5, 3)},
        5: {"if": (f"s#2 == {C:#x}", 6, 1)},
        3: {"do": ["!", f"s#3 = {B:#x}"], "goto": 1},
        4: {"do": ["!"], "stop": True},
        6: {"do": ["!"], "stop": True},
    })
    p = plan(g)
    assert {(t.pred, t.outcome.dest) for t in p.threads if t.split is None} == {(0, 3), (3, 4)}
    assert p.dispatcher == {1, 2} and p.states == {g.key("s#1").var}


def test_a_merge_before_a_threaded_edge_is_not_threaded():
    spec = _tree()  # a lazy resolver: the handler resolves a pointer on first use, then calls through it
    spec[2] = {"do": ["!", f"s#3 = {B:#x}"], "if": ("f#1 == 0", 15, 16)}
    spec[15] = {"do": ["!"], "goto": 16}
    spec[16] = {"do": ["!"], "goto": 9}
    spec[9]["phi"] = {"s#9": {16: "s#3", 3: "s#4", 4: "s#5"}}
    g = toy(spec)
    assert {2, 15} <= {t.pred for t in candidates(g, Values(g), dominators(g))}
    assert {t.pred for t in plan(g).threads} == {0, 16, 3, 4}


# --- negatives ---

def test_a_counted_loop_is_not_threaded():
    g = toy({
        0: {"if": ("p#1 == 0", 1, 2)},
        1: {"do": ["!", "i#1 = 0"], "goto": 3},
        2: {"do": ["i#2 = 0"], "goto": 3},
        3: {"phi": {"i#3": {1: "i#1", 2: "i#2", 4: "i#4"}}, "if": ("i#3 s< 10", 4, 5)},
        4: {"do": ["!", "i#4 = i#3 + 1"], "goto": 3},
        5: {"stop": True},
    })
    assert plan(g) == EMPTY


def test_a_found_flag_is_not_threaded():
    g = toy({
        0: {"do": ["f#1 = 0"], "if": ("x#1 == 5", 1, 2)},
        1: {"do": ["f#2 = 1"], "goto": 2},
        2: {"phi": {"f#3": {0: "f#1", 1: "f#2"}}, "do": ["!"], "if": ("f#3 == 1", 3, 4)},
        3: {"stop": True},
        4: {"stop": True},
    })
    assert plan(g) == EMPTY


def test_a_state_in_memory_is_not_threaded():
    spec = _tree()
    spec[0]["do"] = ["s#1 = ?"]
    for b, k in ((2, 3), (3, 4), (4, 5)):
        spec[b]["do"] = ["!", f"s#{k} = ?"]
    assert plan(toy(spec)) == EMPTY


def test_a_state_from_a_parameter_is_not_threaded():
    g = toy({
        0: {"goto": 1},
        1: {"phi": {"n#2": {0: "n#1", 9: "n#3"}}, "if": (f"s#1 == {A:#x}", 2, 5)},
        5: {"if": (f"s#1 == {B:#x}", 3, 6)},
        6: {"if": (f"s#1 == {C:#x}", 4, 9)},
        2: {"do": ["!"], "goto": 9},
        3: {"do": ["!"], "goto": 9},
        4: {"do": ["!"], "goto": 9},
        9: {"do": ["n#3 = n#2 - 1"], "if": ("n#3 != 0", 1, 8)},
        8: {"stop": True},
    })
    assert plan(g) == EMPTY


# --- rounds and time ---

def test_a_lone_edge_of_an_evidenced_state_is_threaded():
    # What a round leaves is often one edge: later rounds take the dispatcher blocks of the variables
    # evidenced before even when a single thread decides them.
    g = toy({
        0: {"if": ("q#1 u< 4", 5, 4)},
        5: {"do": [f"s#1 = {A:#x}"], "goto": 1},
        4: {"do": ["s#4 = ?"], "goto": 1},
        1: {"phi": {"s#2": {5: "s#1", 4: "s#4"}}, "if": (f"s#2 == {A:#x}", 2, 3)},
        2: {"do": ["!"], "stop": True},
        3: {"do": ["!"], "stop": True},
    })
    assert plan(g) == EMPTY
    carried = plan(g, evidenced=frozenset({g.key("s#1").var}))
    assert _ends(carried) == {(5, 2)} and carried.dispatcher == {1}


def test_evidence_is_carried_to_a_residual_tree():
    g = toy({
        0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
        1: {"phi": {"s#2": {0: "s#1", 9: "s#9"}}, "if": (f"s#2 == {A:#x}", 2, 5)},
        5: {"if": (f"s#2 == {B:#x}", 3, 8)},
        2: {"do": ["!", f"s#3 = {B:#x}"], "goto": 9},
        3: {"do": ["!", f"s#4 = {C:#x}"], "goto": 9},
        8: {"stop": True},
        9: {"phi": {"s#9": {2: "s#3", 3: "s#4"}}, "goto": 1},
    })
    state = g.key("s#1").var
    assert plan(g) == EMPTY
    carried = plan(g, evidenced=frozenset({state}))
    assert _ends(carried) == {(0, 2), (2, 3), (3, 8)} and carried.states == {state}


def test_no_edge_is_simulated_when_no_variable_could_be_a_state(monkeypatch):
    def fail(*args):
        raise AssertionError("simulated")

    monkeypatch.setattr(thread, "simulate", fail)
    g = toy({
        0: {"if": ("p#1 == 0", 1, 2)},
        1: {"do": ["!", "i#1 = 0"], "goto": 3},
        2: {"do": ["i#2 = 0"], "goto": 3},
        3: {"phi": {"i#3": {1: "i#1", 2: "i#2", 4: "i#4"}}, "if": ("i#3 s< 10", 4, 5)},
        4: {"do": ["!", "i#4 = i#3 + 1"], "goto": 3},
        5: {"stop": True},
    })
    assert plan(g) == EMPTY


def test_only_edges_that_can_reach_a_dispatcher_are_simulated(monkeypatch):
    spec = _tree()
    spec[8] = {"do": ["!"], "if": ("p#1 == 0", 11, 12)}  # after the machine: a merge that never reaches it
    spec[11] = {"do": ["!"], "goto": 12}
    spec[12] = {"stop": True}
    starts, real = [], thread.simulate

    def recording(graph, values, pred, start, env):
        starts.append(start)
        return real(graph, values, pred, start, env)

    monkeypatch.setattr(thread, "simulate", recording)
    p = plan(toy(spec))
    assert _ends(p) == {(0, 2), (2, 3), (3, 4), (4, 8)}
    assert 1 in starts and 12 not in starts


def test_an_expired_deadline_gives_an_empty_plan():
    assert plan(toy(_tree()), deadline=Deadline.after(0)) == EMPTY
