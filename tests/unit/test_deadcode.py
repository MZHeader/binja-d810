from binja_d810.binja.deadcode import dead_definitions


def test_definitions_reached_from_a_live_use_are_live():
    pure = {"a": [], "b": ["a"], "c": ["b"], "d": ["a"]}
    assert dead_definitions(pure, roots=["c"]) == {"d"}


def test_chains_of_unused_copies_are_dead_together():
    # x8_1 = x ^ y; var_10 = x8_1; x8_2 = var_10 with nothing reading x8_2.
    pure = {"x8_1": ["x", "y"], "var_10": ["x8_1"], "x8_2": ["var_10"]}
    assert dead_definitions(pure, roots=["ret"]) == {"x8_1", "var_10", "x8_2"}


def test_cycles_terminate():
    assert dead_definitions({"a": ["b"], "b": ["a"]}, roots=["a"]) == set()
    assert dead_definitions({"a": ["b"], "b": ["a"]}, roots=[]) == {"a", "b"}


def test_definitions_die_with_the_root_only_if_every_use_is_inside_it():
    from binja_d810.binja.deadcode import dying_with_root

    # t1 used by root and t2; t2 used only by root; t3 also used by another statement (9).
    uses = {"t1": [5, 3], "t2": [5], "t3": [5, 9]}
    defined_at = {"t1": 2, "t2": 3, "t3": 4}
    assert dying_with_root(uses, defined_at, root_statement=5) == {"t1", "t2"}
    assert dying_with_root({"t": []}, {"t": 1}, root_statement=5) == set()
