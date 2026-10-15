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
