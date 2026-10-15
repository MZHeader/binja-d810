"""Equivalence checking for every rewrite: random evaluation always, Z3 proof when available."""

from __future__ import annotations

import enum
import random
from typing import Iterator, Mapping, Optional

from ..ir.evaluate import evaluate
from ..ir.expr import BinOp, Const, Expr, UnOp, Var, mask, variables


class Verdict(enum.Enum):
    PROVEN = "proven"
    TESTED = "tested"
    REFUTED = "refuted"


def sample_envs(var_widths: Mapping[str, int], count: int, seed: int) -> Iterator[dict[str, int]]:
    rng = random.Random(seed)
    names = sorted(var_widths)
    for i in range(count):
        env = {}
        for j, name in enumerate(names):
            m = mask(var_widths[name])
            edges = (0, 1, m, m >> 1, (m >> 1) + 1)
            env[name] = edges[(i + j) % len(edges)] if i < 2 * len(edges) else rng.getrandbits(var_widths[name])
        yield env


def check_equivalent(
    a: Expr, b: Expr, *, samples: int = 64, use_z3: bool = True, z3_timeout_ms: int = 2000, seed: int = 0
) -> Verdict:
    if a.width != b.width:
        return Verdict.REFUTED
    var_widths = {v.name: v.width for v in variables(a) + variables(b)}
    for env in sample_envs(var_widths, samples, seed):
        if evaluate(a, env) != evaluate(b, env):
            return Verdict.REFUTED
    if use_z3:
        proven = _z3_prove(a, b, var_widths, z3_timeout_ms)
        if proven is False:
            return Verdict.REFUTED
        if proven is True:
            return Verdict.PROVEN
    return Verdict.TESTED


def _z3_prove(a: Expr, b: Expr, var_widths: Mapping[str, int], timeout_ms: int) -> Optional[bool]:
    try:
        import z3
    except ImportError:
        return None
    z3_vars = {name: z3.BitVec(name, width) for name, width in var_widths.items()}
    solver = z3.Solver()
    solver.set("timeout", timeout_ms)
    solver.add(to_z3(a, z3_vars) != to_z3(b, z3_vars))
    result = solver.check()
    if result == z3.unsat:
        return True
    if result == z3.sat:
        return False
    return None


def to_z3(e: Expr, z3_vars: dict):
    import z3

    if isinstance(e, Var):
        return z3_vars[e.name]
    if isinstance(e, Const):
        return z3.BitVecVal(e.value, e.width)
    if isinstance(e, UnOp):
        x = to_z3(e.operand, z3_vars)
        if e.op == "neg":
            return -x
        if e.op == "not":
            return ~x
        if e.op == "zx":
            return z3.ZeroExt(e.width - e.operand.width, x)
        if e.op == "sx":
            return z3.SignExt(e.width - e.operand.width, x)
        return z3.Extract(e.width - 1, 0, x)  # trunc
    assert isinstance(e, BinOp)
    x = to_z3(e.left, z3_vars)
    y = to_z3(e.right, z3_vars)
    if e.op in ("shl", "lshr", "ashr"):
        if e.right.width < e.width:
            y = z3.ZeroExt(e.width - e.right.width, y)
        elif e.right.width > e.width:
            y = z3.Extract(e.width - 1, 0, y)
        y = z3.URem(y, z3.BitVecVal(e.width, e.width))
        if e.op == "shl":
            return x << y
        if e.op == "lshr":
            return z3.LShR(x, y)
        return x >> y
    return {
        "add": lambda: x + y, "sub": lambda: x - y, "mul": lambda: x * y,
        "and": lambda: x & y, "or": lambda: x | y, "xor": lambda: x ^ y,
    }[e.op]()
