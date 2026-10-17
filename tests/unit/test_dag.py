"""Shared values (v3.1): a lifted condition reuses SSA values, so its expression is a DAG whose tree
can be exponentially larger. Everything the constancy path does with it must stay linear."""

import time

from binja_d810.ir.evaluate import evaluate, evaluate_shared
from binja_d810.ir.expr import BinOp, Const, UnOp, Var, brief, distinct_nodes, tree_size
from binja_d810.ir.parse import parse

x = Var("x", 32)
MASKS = [(0x9E3779B9 * (i + 1)) & 0xFFFFFFFF for i in range(30)]


def chain(depth: int = 30):
    """t' = (t & m) | (~t & ~m) from p = x * (x - 1): each step reads t twice."""
    t = BinOp("mul", x, BinOp("sub", x, Const(1, 32), 32), 32)
    for m in MASKS[:depth]:
        t = BinOp("or", BinOp("and", t, Const(m, 32), 32),
                  BinOp("and", UnOp("not", t, 32), Const(~m & 0xFFFFFFFF, 32), 32), 32)
    return t


def timed(f):
    start = time.perf_counter()
    value = f()
    return value, time.perf_counter() - start


def test_sizes_count_the_tree_and_the_shared_nodes():
    e = chain()
    assert distinct_nodes(e) < 300
    assert tree_size(e) > 2 ** 30
    small = parse("(a & b) + (a & b)", 32)
    assert tree_size(small) == 7


def test_hashing_and_comparison_are_linear():
    a, b = chain(), chain()  # equal, built separately: no shared objects between them
    (_, seconds) = timed(lambda: (hash(a), hash(b), a == b, a != chain(29)))
    assert a == b and hash(a) == hash(b) and a != chain(29)
    assert seconds < 0.5, seconds


def test_evaluation_is_linear_and_agrees_with_the_tree_evaluator():
    value, seconds = timed(lambda: evaluate_shared(chain(), {"x": 12345}))
    assert seconds < 0.5, seconds
    k = 0
    for m in MASKS:
        k ^= ~m & 0xFFFFFFFF
    assert value == ((12345 * 12344) & 0xFFFFFFFF) ^ k  # each step is t ^ ~m
    small = parse("((a ^ b) + 2 * (a & b)) | ~c", 32)
    env = {"a": 7, "b": 0xFFFFFFF0, "c": 3}
    assert evaluate_shared(small, env) == evaluate(small, env)


def test_z3_terms_are_built_once_per_shared_value():
    from binja_d810.semantic.verify import to_z3

    import z3

    _, seconds = timed(lambda: to_z3(chain(), {"x": z3.BitVec("x", 32)}))
    assert seconds < 0.5, seconds


def test_brief_text_is_bounded():
    text, seconds = timed(lambda: brief(chain(), limit=200))
    assert seconds < 0.5 and len(text) <= 260, text
    assert brief(parse("a + 1", 32)) == "(a + 1)"


def test_repr_is_bounded():
    text, seconds = timed(lambda: repr(chain()))
    assert seconds < 0.5 and len(text) < 400, text[:400]
    assert repr(parse("a + 1", 32)) == "BinOp(op='add', left=Var(name='a', width=32), right=Const(value=1, width=32), width=32)"
