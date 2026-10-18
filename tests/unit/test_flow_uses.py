from binja_d810.flow.uses import ESCAPE, READ, WRITE, classify, reaches_target


class Graph:
    """A use graph from tables: kinds by node, (parent, operand) by node, readers by defining node."""

    def __init__(self, kinds, parents, readers=None):
        self.kinds, self.parents, self.readers_of = kinds, parents, readers or {}

    def kind(self, node):
        return self.kinds[node]

    def parent(self, node):
        return self.parents.get(node)

    def readers(self, node):
        return self.readers_of.get(node, [])


def _copies(n, end_kind="load", end_operand="src"):
    """c -> set_var s1 -> var v1 -> set_var s2 -> ... -> var vn -> end."""
    kinds, parents, readers = {"c": "const", "end": end_kind}, {}, {}
    node = "c"
    for i in range(1, n + 1):
        kinds[f"s{i}"], kinds[f"v{i}"] = "set_var", "var"
        parents[node] = (f"s{i}", "src")
        readers[f"s{i}"] = [f"v{i}"]
        node = f"v{i}"
    parents[node] = ("end", end_operand)
    return Graph(kinds, parents, readers)


def test_a_load_through_address_arithmetic_is_a_read():
    g = Graph({"c": "const", "shl": "shl", "add": "add", "ld": "load"},
              {"c": ("add", "left"), "add": ("ld", "src")})
    assert classify(["c"], g) == (READ, None)


def test_a_store_destination_is_a_write():
    g = Graph({"c": "const", "st": "store"}, {"c": ("st", "dest")})
    assert classify(["c"], g) == (WRITE, "st")


def test_a_stored_value_escapes():
    g = Graph({"c": "const", "st": "store"}, {"c": ("st", "src")})
    assert classify(["c"], g) == (ESCAPE, "st")


def test_a_call_argument_escapes():
    g = Graph({"c": "const", "call": "call"}, {"c": ("call", "params")})
    assert classify(["c"], g) == (ESCAPE, "call")


def test_a_comparison_escapes():
    g = Graph({"c": "const", "cmp": "mlil_cmp_e"}, {"c": ("cmp", "left")})
    assert classify(["c"], g) == (ESCAPE, "cmp")


def test_copies_are_followed_up_to_the_depth_limit():
    assert classify(["c"], _copies(8)) == (READ, None)
    assert classify(["c"], _copies(9)) == (ESCAPE, "s9")


def test_a_write_through_a_copy_is_a_write():
    assert classify(["c"], _copies(2, "store", "dest")) == (WRITE, "end")


def test_a_copy_nobody_reads_is_harmless():
    g = Graph({"c": "const", "s": "set_var"}, {"c": ("s", "src")}, {"s": []})
    assert classify(["c"], g) == (READ, None)


def test_a_merge_is_followed():
    g = Graph({"c": "const", "s": "set_var", "phi": "phi", "v": "var", "ld": "load"},
              {"c": ("s", "src"), "v": ("ld", "src")}, {"s": ["phi"], "phi": ["v"]})
    assert classify(["c"], g) == (READ, None)


def test_a_partial_read_of_a_copy_escapes():
    g = Graph({"c": "const", "s": "set_var", "f": "field", "ld": "load"},
              {"c": ("s", "src"), "f": ("ld", "src")}, {"s": ["f"]})
    assert classify(["c"], g) == (ESCAPE, "f")


def test_a_statement_of_its_own_escapes():
    assert classify(["c"], Graph({"c": "const"}, {})) == (ESCAPE, "c")


def test_every_start_must_only_read():
    g = Graph({"c1": "const", "c2": "const", "ld": "load", "st": "store"},
              {"c1": ("ld", "src"), "c2": ("st", "dest")})
    assert classify(["c1", "c2"], g) == (WRITE, "st")


def test_no_use_at_all_is_a_read():
    assert classify([], Graph({}, {})) == (READ, None)


def test_a_result_used_as_a_call_target():
    assert reaches_target(["v"], Graph({"v": "var", "call": "call"}, {"v": ("call", "dest")}))


def test_a_result_copied_into_a_jump_target():
    g = Graph({"v": "var", "s": "set_var", "w": "var", "j": "jump"},
              {"v": ("s", "src"), "w": ("j", "dest")}, {"s": ["w"]})
    assert reaches_target(["v"], g)


def test_a_result_used_as_an_argument_or_in_arithmetic_is_not_a_target():
    assert not reaches_target(["v"], Graph({"v": "var", "call": "call"}, {"v": ("call", "params")}))
    g = Graph({"v": "var", "add": "add", "call": "call"}, {"v": ("add", "left"), "add": ("call", "dest")})
    assert not reaches_target(["v"], g)
