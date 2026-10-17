"""Per-path variants over merged values (spec v3.2 goal 3), independent of Binary Ninja."""

from binja_d810.deadline import Deadline
from binja_d810.ir.expr import BinOp, Const, Var, variables
from binja_d810.ir.paths import Merge, path_variants
from binja_d810.semantic.constancy import fold_shared
from binja_d810.semantic.verify import Verdict

W = 32


def v(name: str, width: int = W) -> Var:
    return Var(name, width, ref=name)  # the ref stands for the SSA variable


def cmp(op: str, a, b):
    return BinOp(op, a, b, 1)


def merges(table: dict):
    """merge_of from {leaf name: Merge}, by the leaf's reference (renamed leaves keep it)."""
    return lambda leaf: table.get(leaf.ref)


def constant(e):
    found = fold_shared(e, Deadline.never())
    return found[0] if found is not None and found[1].verdict is Verdict.PROVEN else None


def test_nothing_to_split():
    assert path_variants(cmp("eq", v("x"), Const(1, W)), merges({})) is None


def test_merges_of_one_block_take_the_same_edge():
    # a = phi(y > 9, 1), b = phi(y < 10, 0) in block 7: a ^ b is 1 on both paths, though neither
    # a nor b is constant, and the mixed pairs ((y > 9) ^ 0) are not.
    y = v("y")
    a, b = v("a", 1), v("b", 1)
    table = {"a": Merge(7, (cmp("sgt", y, Const(9, W)), Const(1, 1))),
             "b": Merge(7, (cmp("slt", y, Const(10, W)), Const(0, 1)))}
    variants = path_variants(BinOp("xor", a, b, 1), merges(table))
    assert variants is not None and len(variants) == 2
    assert [constant(e) for e in variants] == [1, 1]


def test_a_value_from_an_earlier_iteration_is_not_the_current_one():
    # p = phi(a, L + 1) at a loop head; the back edge's L + 1 was computed from the previous
    # iteration's L. p == L + 1 is not constant: the incoming L is renamed apart from the current L.
    L = v("L")
    p = v("p")
    table = {"p": Merge(2, (v("a"), BinOp("add", L, Const(1, W), W)))}
    variants = path_variants(cmp("eq", p, BinOp("add", L, Const(1, W), W)), merges(table))
    assert variants is not None and len(variants) == 2
    assert all(constant(e) is None for e in variants)


def test_x_tunnel_quotient_against_a_merged_square():
    # i = phi(y1 * y1 + 3, y2 * y2 + 3): 2 / (x * x + 1) == i is false on each path.
    x, i = v("x"), v("i")
    square = lambda y: BinOp("add", BinOp("mul", y, y, W), Const(3, W), W)  # noqa: E731
    quotient = BinOp("udiv", Const(2, W), BinOp("add", BinOp("mul", x, x, W), Const(1, W), W), W)
    table = {"i": Merge(4, (square(v("y1")), square(v("y2"))))}
    condition = cmp("eq", quotient, i)
    assert constant(condition) is None
    variants = path_variants(condition, merges(table))
    assert variants is not None and [constant(e) for e in variants] == [0, 0]


def test_merges_inside_incoming_values_are_split_two_deep():
    # p = phi(q, 4) with q = phi(6, 8): p is even on every path (three of them).
    p, q = v("p"), v("q")
    table = {"p": Merge(1, (q, Const(4, W))), "q": Merge(0, (Const(6, W), Const(8, W)))}
    condition = cmp("eq", BinOp("and", p, Const(1, W), W), Const(0, W))
    variants = path_variants(condition, merges(table))
    assert variants is not None and [constant(e) for e in variants] == [1, 1, 1]
    # One level only: q stays a free leaf on p's first edge.
    shallow = path_variants(condition, merges(table), depth=1)
    assert shallow is not None and constant(shallow[0]) is None and constant(shallow[1]) == 1


def test_too_many_paths():
    leaves = [v(f"m{k}") for k in range(5)]
    table = {f"m{k}": Merge(k, (Const(0, W), Const(2, W))) for k in range(5)}  # 2^5 = 32 paths
    total = leaves[0]
    for leaf in leaves[1:]:
        total = BinOp("add", total, leaf, W)
    assert path_variants(cmp("eq", BinOp("and", total, Const(1, W), W), Const(0, W)), merges(table)) is None
    assert path_variants(cmp("eq", BinOp("and", total, Const(1, W), W), Const(0, W)), merges(table), limit=32)


def test_an_invariant_merge_is_its_one_source():
    # x-tunnel 0x4485a0 (v3.3): i is merged round a loop with copies of itself, so its only value from
    # outside is y * y + 3 (Merge with one incoming value). It is substituted, renamed apart, without
    # multiplying the variants; x, merged from two values, may stay a free leaf.
    x, i = v("x"), v("i")
    quotient = BinOp("udiv", Const(2, W), BinOp("add", BinOp("mul", x, x, W), Const(1, W), W), W)
    square = BinOp("add", BinOp("mul", v("y"), v("y"), W), Const(3, W), W)
    table = {"i": Merge(("web", "i"), (square,)), "x": Merge(3, (v("a"), v("b")))}
    condition = cmp("eq", quotient, i)
    invariant = path_variants(condition, merges(table), invariant_only=True)
    assert invariant is not None and len(invariant) == 1 and constant(invariant[0]) == 0
    assert "x" in {leaf.name for leaf in variables(invariant[0])}  # x stays free: not split
    split = path_variants(condition, merges(table))
    assert split is not None and len(split) == 2 and [constant(e) for e in split] == [0, 0]


def test_invariant_only_without_an_invariant_merge():
    x = v("x")
    table = {"x": Merge(3, (Const(1, W), Const(3, W)))}
    assert path_variants(cmp("eq", BinOp("and", x, Const(1, W), W), Const(1, W)), merges(table),
                         invariant_only=True) is None
