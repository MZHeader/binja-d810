"""Equivalence checking for every rewrite: random evaluation always, then a proof.

The proof is by signature when both sides are linear MBAs (fast and exact), else by Z3 when
available. Z3 rarely proves MBA equivalences quickly, so its default budget is 500 ms; a
timeout leaves the random-evaluation result (TESTED).
"""

from __future__ import annotations

import enum
import random
import threading
from dataclasses import dataclass
from typing import Iterator, Mapping, Optional

from ..deadline import Deadline
from ..ir.evaluate import evaluate
from ..ir.expr import CMP_OPS, BinOp, Const, Expr, UnOp, Var, mask, variables
from ..ir.normalize import normalize
from .linear_mba import abstract_atoms, is_linear_mba, signature

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


@dataclass(frozen=True)
class Check:
    """A verdict with how it was reached: method is "signature", "z3" or "sampling"; reason says
    why a TESTED verdict has no proof ("z3 disabled", "z3 unavailable", "z3 timeout", ...)."""

    verdict: Verdict
    method: str
    reason: str = ""


def check(
    a: Expr, b: Expr, *, samples: int = 64, use_z3: bool = True, z3_timeout_ms: int = 500, seed: int = 0,
    deadline: Optional[Deadline] = None,
) -> Check:
    if a.width != b.width:
        return Check(Verdict.REFUTED, "width", "")
    var_widths = {v.name: v.width for v in variables(a) + variables(b)}
    for env in sample_envs(var_widths, samples, seed):
        if evaluate(a, env) != evaluate(b, env):
            return Check(Verdict.REFUTED, "sampling")
    # Proofs work on normalized forms (normalization is an exact identity, and turns x << 1 into
    # 2 * x and trunc32(zx64(y)) into y); the sampling above checked the forms as given.
    a, b = normalize(a), normalize(b)
    by_signature = _signature_proof(a, b)
    if by_signature is not None:
        return Check(Verdict.PROVEN if by_signature else Verdict.REFUTED, "signature")
    if _atom_signature_proof(a, b):
        return Check(Verdict.PROVEN, "signature")
    if isinstance(a, BinOp) and a.op in ("eq", "ne") and isinstance(b, Const) and b.value == int(a.op == "eq"):
        # eq(L, R) is always 1 (ne always 0) exactly when L == R is an identity, which signatures
        # prove for MBAs where Z3 on the comparison often times out.
        inner = check(a.left, a.right, samples=samples, use_z3=use_z3, z3_timeout_ms=z3_timeout_ms, seed=seed,
                      deadline=deadline)
        if inner.verdict is not Verdict.TESTED:
            return inner
    if not use_z3:
        return Check(Verdict.TESTED, "sampling", "z3 disabled")
    if deadline is not None and deadline.at != float("inf"):
        remaining_ms = deadline.remaining() * 1000
        if remaining_ms < 2:
            return Check(Verdict.TESTED, "sampling", "proof budget exhausted")
        z3_timeout_ms = min(z3_timeout_ms, int(remaining_ms))
    proven, reason = _z3_prove(a, b, var_widths, z3_timeout_ms)
    if proven is False:
        return Check(Verdict.REFUTED, "z3")
    if proven is True:
        return Check(Verdict.PROVEN, "z3")
    return Check(Verdict.TESTED, "sampling", reason)


def check_equivalent(
    a: Expr, b: Expr, *, samples: int = 64, use_z3: bool = True, z3_timeout_ms: int = 500, seed: int = 0
) -> Verdict:
    return check(a, b, samples=samples, use_z3=use_z3, z3_timeout_ms=z3_timeout_ms, seed=seed).verdict


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


def _atom_signature_proof(a: Expr, b: Expr) -> bool:
    """True if both sides, with non-linear subterms replaced by shared fresh variables (atoms), are
    linear MBAs with the same signature: the identity then holds for every value of every atom, so
    for the subterms too ((A ^ B) + 2 * (~A | B) == B - A - 2 with A, B sums, which Z3 does not
    prove quickly). False proves nothing: atoms can be related (x + 1 and x)."""
    atoms: dict = {}
    taken = {v.name for v in variables(a) + variables(b)}
    abstract_atoms(a, atoms, taken)
    abstract_atoms(b, atoms, taken)
    if not atoms:
        return False  # no atom: _signature_proof already decided whatever it could
    # Every occurrence of an atom's subterm, also where it is linear ((z - w) - (x + y) - 2).
    return _signature_proof(_replace_atoms(a, atoms), _replace_atoms(b, atoms)) is True


def _replace_atoms(e: Expr, atoms: dict) -> Expr:
    if e in atoms:
        return atoms[e]
    if isinstance(e, BinOp):
        return BinOp(e.op, _replace_atoms(e.left, atoms), _replace_atoms(e.right, atoms), e.width)
    if isinstance(e, UnOp):
        return UnOp(e.op, _replace_atoms(e.operand, atoms), e.width)
    return e


def _z3_prove(a: Expr, b: Expr, var_widths: Mapping[str, int], timeout_ms: int) -> tuple[Optional[bool], str]:
    """(True, "") proven, (False, "") refuted, (None, reason) inconclusive."""
    try:
        import z3
    except ImportError:
        return None, "z3 unavailable"
    ctx = _z3_context()
    z3_vars = {name: z3.BitVec(name, width, ctx) for name, width in var_widths.items()}
    solver = z3.Solver(ctx=ctx)
    solver.set("timeout", max(1, int(timeout_ms)))
    solver.add(to_z3(a, z3_vars, ctx) != to_z3(b, z3_vars, ctx))
    result = solver.check()
    if result == z3.unsat:
        return True, ""
    if result == z3.sat:
        return False, ""
    why = solver.reason_unknown()
    return None, "z3 timeout" if why in ("timeout", "canceled") else f"z3 unknown: {why}"


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
    if e.op in CMP_OPS:
        cond = {"eq": lambda: x == y, "ne": lambda: x != y, "ult": lambda: z3.ULT(x, y),
                "ule": lambda: z3.ULE(x, y), "ugt": lambda: z3.UGT(x, y), "uge": lambda: z3.UGE(x, y),
                "slt": lambda: x < y, "sle": lambda: x <= y, "sgt": lambda: x > y, "sge": lambda: x >= y}[e.op]()
        return z3.If(cond, z3.BitVecVal(1, 1, ctx), z3.BitVecVal(0, 1, ctx))
    if e.op in ("rol", "ror"):
        # ir.evaluate._rotate: the count modulo the width, taken at the count's own width.
        cw = e.right.width
        if cw < e.width:
            y = z3.ZeroExt(e.width - cw, y)
        elif cw > e.width:
            y = z3.Extract(e.width - 1, 0, z3.URem(y, z3.BitVecVal(e.width, cw, ctx)))
        y = z3.URem(y, z3.BitVecVal(e.width, e.width, ctx))
        return z3.RotateLeft(x, y) if e.op == "rol" else z3.RotateRight(x, y)
    if e.op in ("shl", "lshr", "ashr"):
        # ir.evaluate.shift_count: the count modulo max(32, width), taken at the count's own width.
        # The reduced count is at most 63, so resizing it to the result width is lossless; Z3's
        # shifts then clear (or sign-fill) for a reduced count >= the width, as an 8-bit shift does.
        y = y & z3.BitVecVal(max(32, e.width) - 1, e.right.width, ctx)
        if e.right.width < e.width:
            y = z3.ZeroExt(e.width - e.right.width, y)
        elif e.right.width > e.width:
            y = z3.Extract(e.width - 1, 0, y)
        if e.op == "shl":
            return x << y
        if e.op == "lshr":
            return z3.LShR(x, y)
        return x >> y
    return {
        "add": lambda: x + y, "sub": lambda: x - y, "mul": lambda: x * y,
        "and": lambda: x & y, "or": lambda: x | y, "xor": lambda: x ^ y,
    }[e.op]()
