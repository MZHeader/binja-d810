"""Seeded random IR expressions for property tests."""

import random

from binja_d810.ir.expr import BinOp, Const, Expr, UnOp, Var

DEFAULT_OPS = ("add", "sub", "mul", "and", "or", "xor", "neg", "not")


def random_expr(rng: random.Random, names=("a", "b", "c"), width=32, depth=4, ops=DEFAULT_OPS) -> Expr:
    if depth == 0 or rng.random() < 0.25:
        if rng.random() < 0.8:
            return Var(rng.choice(names), width)
        return Const(rng.choice([0, 1, 2, 3, 0xFF, -1, rng.getrandbits(width)]), width)
    op = rng.choice(ops)
    if op in ("neg", "not"):
        return UnOp(op, random_expr(rng, names, width, depth - 1, ops), width)
    if op == "shl":
        return BinOp(op, random_expr(rng, names, width, depth - 1, ops), Const(rng.randrange(width), width), width)
    return BinOp(op, random_expr(rng, names, width, depth - 1, ops), random_expr(rng, names, width, depth - 1, ops), width)
