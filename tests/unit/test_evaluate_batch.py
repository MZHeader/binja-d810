"""Shared values evaluated on a batch of samples in one walk (v3.3): the same values as evaluate."""

import random

from exprgen import random_expr

from binja_d810.ir.evaluate import evaluate, evaluate_shared_batch
from binja_d810.ir.expr import CMP_OPS, BinOp, Const, UnOp, Var

OPS = ("add", "sub", "mul", "and", "or", "xor", "neg", "not", "shl", "lshr", "ashr", "rol", "ror",
       "udiv", "urem", "sdiv", "srem")


def _envs(rng, n=40):
    return [{name: rng.choice([0, 1, 0xFF, 0x80000000, 0xFFFFFFFF, rng.getrandbits(32)]) for name in "abc"}
            for _ in range(n)]


def test_agrees_with_evaluate_on_random_expressions():
    rng = random.Random(7)
    for _ in range(300):
        e = random_expr(rng, depth=5, ops=OPS)
        envs = _envs(rng)
        assert evaluate_shared_batch(e, envs) == [evaluate(e, env) for env in envs], e


def test_comparisons_extensions_and_truncations():
    rng = random.Random(8)
    a, b = Var("a", 32), Var("b", 32)
    exprs = [BinOp(op, a, b, 1) for op in sorted(CMP_OPS)]
    exprs += [UnOp("zx", BinOp("ult", a, b, 1), 8), UnOp("sx", UnOp("trunc", a, 8), 32), UnOp("trunc", a, 16),
              BinOp("shl", a, UnOp("trunc", b, 8), 32), BinOp("add", UnOp("zx", UnOp("trunc", a, 8), 32), Const(7, 32), 32)]
    envs = _envs(rng)
    for e in exprs:
        assert evaluate_shared_batch(e, envs) == [evaluate(e, env) for env in envs], e


def test_shared_values_are_walked_once():
    t = Var("a", 32)
    for i in range(30):  # 2^30 nodes as a tree
        m = Const(0x9E3779B9 * (i + 1), 32)
        t = BinOp("or", BinOp("and", t, m, 32), BinOp("and", UnOp("not", t, 32), UnOp("not", m, 32), 32), 32)
    envs = [{"a": v} for v in (0, 1, 0xDEADBEEF)]
    assert len(evaluate_shared_batch(t, envs)) == 3


def test_an_empty_batch():
    assert evaluate_shared_batch(Var("a", 32), []) == []
