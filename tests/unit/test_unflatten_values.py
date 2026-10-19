"""Static values and value sets over the unflattening graph model (v4.2, spec §3.2)."""

from toygraph import toy

from binja_d810.ir.expr import Var
from binja_d810.unflatten.graph import dominators, reaches
from binja_d810.unflatten.values import Values

A, B = 0x5a5a1001, 0x5a5a2002


def test_static_of_a_constant_and_of_an_expression_over_it():
    g = toy({0: {"do": ["s#1 = 0x10", "t#1 = s#1 + 1"], "stop": True}})
    v = Values(g)
    assert v.static(g.key("s#1")) == 0x10 and v.static(g.key("t#1")) == 0x11


def test_static_of_a_phi_of_equal_constants():
    g = toy({0: {"do": ["s#1 = 0x10", "s#2 = 0x10"], "goto": 1},
             1: {"phi": {"s#3": {0: "s#1", 2: "s#2"}}, "stop": True}})
    assert Values(g).static(g.key("s#3")) == 0x10


def test_no_static_value_for_unequal_constants():
    g = toy({0: {"do": ["s#1 = 0x10", "s#2 = 0x20"], "goto": 1},
             1: {"phi": {"s#3": {0: "s#1", 2: "s#2"}}, "stop": True}})
    assert Values(g).static(g.key("s#3")) is None


def test_no_static_value_for_a_phi_cycle():
    g = toy({0: {"do": ["s#1 = 0x10"], "goto": 1},
             1: {"phi": {"s#2": {0: "s#1", 1: "s#3"}}, "do": ["s#3 = s#2"], "goto": 1}})
    assert Values(g).static(g.key("s#2")) is None


def test_value_set_of_a_phi_of_two_constants():
    g = toy({0: {"do": [f"s#1 = {A:#x}", f"s#2 = {B:#x}"], "goto": 1},
             1: {"phi": {"s#3": {0: "s#1", 2: "s#2"}}, "stop": True}})
    assert Values(g).value_set(g.key("s#3")) == {A, B}


def test_value_set_through_a_phi_cycle():
    g = toy({0: {"do": [f"s#1 = {A:#x}"], "goto": 1},
             1: {"phi": {"s#2": {0: "s#1", 1: "s#3"}}, "do": ["s#3 = s#2"], "goto": 1}})
    assert Values(g).value_set(g.key("s#2")) == {A}


def test_value_set_of_a_select_over_a_flag():
    g = toy({0: {"do": ["s#1 = ((0 - zx32(c#1)) & 0x10) + 0x20"], "stop": True}}, widths={"c": 1})
    assert Values(g).value_set(g.key("s#1")) == {0x20, 0x30}


def test_value_set_of_three_flags():
    g = toy({0: {"do": ["s#1 = zx32(p#1) + zx32(q#1) + zx32(r#1)"], "stop": True}},
            widths={"p": 1, "q": 1, "r": 1})
    assert Values(g).value_set(g.key("s#1")) == {0, 1, 2, 3}


def test_no_value_set_for_four_flags():
    g = toy({0: {"do": ["s#1 = zx32(p#1) + zx32(q#1) + zx32(r#1) + zx32(u#1)"], "stop": True}},
            widths={"p": 1, "q": 1, "r": 1, "u": 1})
    assert Values(g).value_set(g.key("s#1")) is None


def test_no_value_set_for_an_impure_leaf():
    g = toy({0: {"do": [f"s#1 = {A:#x}", "s#2 = ?"], "goto": 1},
             1: {"phi": {"s#3": {0: "s#1", 2: "s#2"}}, "stop": True}})
    assert Values(g).value_set(g.key("s#3")) is None


def test_no_value_set_for_a_parameter_leaf():
    g = toy({0: {"do": ["s#2 = s#1 + 1"], "stop": True}})
    assert Values(g).value_set(g.key("s#2")) is None


def test_no_value_set_beyond_sixteen_values():
    g = toy({0: {"do": [f"s#{i} = {i:#x}" for i in range(1, 18)], "goto": 1},
             1: {"phi": {"s#20": {i: f"s#{i}" for i in range(1, 18)}}, "stop": True}})
    v = Values(g)
    assert v.value_set(g.key("s#20")) is None


def test_eval_reads_the_environment_before_static_values():
    g = toy({0: {"do": ["s#1 = 0x10", "t#1 = s#1 + 1"], "stop": True}})
    v = Values(g)
    assert v.eval(g.expr("s#1 + 1"), {g.key("s#1"): 0x40}) == 0x41
    assert v.eval(g.expr("s#1 + 1"), {}) == 0x11
    assert v.eval(g.expr("x#1 + 1"), {}) is None


def test_a_narrow_leaf_reads_the_low_bits():
    g = toy({0: {"stop": True}})
    key = g.key("s#1")
    assert Values(g).eval(Var("s#1", 16, ref=key), {key: 0x12345678}) == 0x5678


def _loop():
    return toy({0: {"goto": 1}, 1: {"if": ("x#1 == 0", 2, 3)}, 2: {"goto": 4}, 3: {"goto": 4},
                4: {"if": ("x#1 == 1", 1, 5)}, 5: {"stop": True}})


def test_dominators_of_a_diamond_in_a_loop():
    doms = dominators(_loop())
    assert doms[4] == {0, 1, 4} and doms[5] == {0, 1, 4, 5} and doms[2] == {0, 1, 2}


def test_reaches_without_an_edge():
    g = _loop()
    assert not reaches(g, 2, 3, without=(1, 3))
    assert reaches(g, 2, 3, without=(4, 5))
    assert reaches(g, 4, 4, without=(4, 1))
