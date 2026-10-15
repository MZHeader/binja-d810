"""Equivalence checking for every rewrite: random evaluation always, then a proof.

The proof is by signature when both sides are linear MBAs (fast and exact), else by Z3 when
available. Z3 rarely proves MBA equivalences quickly, so its default budget is 500 ms; a
timeout leaves the random-evaluation result (TESTED).
"""

from __future__ import annotations

import enum
import random
import threading
from typing import Iterator, Mapping, Optional

from ..ir.evaluate import evaluate
from ..ir.expr import BinOp, Const, Expr, UnOp, Var, mask, variables
from .linear_mba import is_linear_mba, signature

_SIGNATURE_MAX_VARS = 12
_Z3_LOCAL = threading.local()
_Z3_CREATE = threading.Lock()


def _z3_context():
    """This thread's Z3 context.

    Binary Ninja runs the activity on parallel analysis threads, and Z3 contexts (the default
    one included) are not thread-safe. Creation is serialized as well.
    """
    ctx = getattr(_Z3_LOCAL, "ctx", None)
    if ctx is None:
        import z3

        with _Z3_CREATE:
            ctx = z3.Context()
        _Z3_LOCAL.ctx = ctx
    return ctx


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
    a: Expr, b: Expr, *, samples: int = 64, use_z3: bool = True, z3_timeout_ms: int = 500, seed: int = 0
) -> Verdict:
    if a.width != b.width:
        return Verdict.REFUTED
    var_widths = {v.name: v.width for v in variables(a) + variables(b)}
    for env in sample_envs(var_widths, samples, seed):
        if evaluate(a, env) != evaluate(b, env):
            return Verdict.REFUTED
    by_signature = _signature_proof(a, b)
    if by_signature is not None:
        return Verdict.PROVEN if by_signature else Verdict.REFUTED
    if use_z3:
        proven = _z3_prove(a, b, var_widths, z3_timeout_ms)
        if proven is False:
            return Verdict.REFUTED
        if proven is True:
            return Verdict.PROVEN
    return Verdict.TESTED


def _signature_proof(a: Expr, b: Expr) -> Optional[bool]:
    """Exact verdict for two linear MBAs whose variables all have the expression's width.

    Each bit of such an expression depends only on the same bit of its inputs, so the expression
    is determined by its values on 0/1 inputs. None when the precondition does not hold.
    """
    vs = sorted({v.name: v for v in variables(a) + variables(b)}.values(), key=lambda v: v.name)
    if len(vs) > _SIGNATURE_MAX_VARS or any(v.width != a.width for v in vs):
        return None
    if not (is_linear_mba(a) and is_linear_mba(b)):
        return None
    return signature(a, vs) == signature(b, vs)


def _z3_prove(a: Expr, b: Expr, var_widths: Mapping[str, int], timeout_ms: int) -> Optional[bool]:
    try:
        import z3
    except ImportError:
        return None
    ctx = _z3_context()
    z3_vars = {name: z3.BitVec(name, width, ctx) for name, width in var_widths.items()}
    solver = z3.Solver(ctx=ctx)
    solver.set("timeout", timeout_ms)
    solver.add(to_z3(a, z3_vars, ctx) != to_z3(b, z3_vars, ctx))
    result = solver.check()
    if result == z3.unsat:
        return True
    if result == z3.sat:
        return False
    return None


def to_z3(e: Expr, z3_vars: dict, ctx=None):
    import z3

    if isinstance(e, Var):
        return z3_vars[e.name]
    if isinstance(e, Const):
        return z3.BitVecVal(e.value, e.width, ctx)
    if isinstance(e, UnOp):
        x = to_z3(e.operand, z3_vars, ctx)
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
    x = to_z3(e.left, z3_vars, ctx)
    y = to_z3(e.right, z3_vars, ctx)
    if e.op in ("shl", "lshr", "ashr"):
        if e.right.width < e.width:
            y = z3.ZeroExt(e.width - e.right.width, y)
        elif e.right.width > e.width:
            y = z3.Extract(e.width - 1, 0, y)
        y = z3.URem(y, z3.BitVecVal(e.width, e.width, ctx))
        if e.op == "shl":
            return x << y
        if e.op == "lshr":
            return z3.LShR(x, y)
        return x >> y
    return {
        "add": lambda: x + y, "sub": lambda: x - y, "mul": lambda: x * y,
        "and": lambda: x & y, "or": lambda: x | y, "xor": lambda: x ^ y,
    }[e.op]()
