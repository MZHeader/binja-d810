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


def test_single_use_definitions_are_shown_in_the_statement_that_uses_them():
    from binja_d810.binja.deadcode import shown_in

    # mba_challenge (three-address MLIL): t1 = x ^ y; t2 = x & y; t3 = t1 + t2 * 2; root = t3 | z.
    # HLIL shows one statement, so the chain is one (mixed) statement.
    uses = {"t1": [3], "t2": [3], "t3": [4]}
    defined_at = {"t1": 1, "t2": 2, "t3": 3}
    assert shown_in({"t1", "t2", "t3"}, uses, defined_at, root_statement=4) == {"t1": None, "t2": None, "t3": None}
    # snagit.dll: a = x * 7 + y is used twice (t1 = a & M; t2 = ~a), so HLIL shows it on its own.
    uses = {"a": [2, 3], "t1": [5], "t2": [4], "t3": [5]}
    defined_at = {"a": 1, "t1": 2, "t2": 3, "t3": 4}
    assert shown_in({"a", "t1", "t2", "t3"}, uses, defined_at, root_statement=5) == {
        "a": "a", "t1": None, "t2": None, "t3": None}
    # A single-use definition used by a statement that stands alone is shown in that one.
    uses = {"m": [2], "a": [3, 4]}
    defined_at = {"m": 1, "a": 2}
    assert shown_in({"m", "a"}, uses, defined_at, root_statement=5) == {"m": "a", "a": "a"}


def test_a_check_runs_only_for_a_definition_that_would_be_removed():
    from binja_d810.binja.deadcode import dead_with_checks

    ran = []
    checks = {"live": lambda: ran.append("live") or True, "dead": lambda: ran.append("dead") or True}
    pure = {"live": ["a"], "dead": ["b"], "a": [], "b": []}
    assert dead_with_checks(pure, ["live"], checks) == {"dead", "b"}
    assert ran == ["dead"]


def test_a_failed_check_keeps_the_definition_and_what_it_reads():
    from binja_d810.binja.deadcode import dead_with_checks

    # q = a / b where b may be 0: not removable, so a and b stay alive.
    pure = {"q": ["a", "b"], "a": [], "b": [], "t": []}
    assert dead_with_checks(pure, [], {"q": lambda: False}) == {"t"}
