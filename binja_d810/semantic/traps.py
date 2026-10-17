"""Can a division trap?

x86's div and idiv trap on a zero divisor, and idiv on MIN / -1 (the quotient overflows). A division
is lifted, and may then be evaluated, folded, moved or removed, only if no value of its inputs makes
it trap: a nonzero constant divisor (signed: not -1), or a proof. The proof quantifies over every
value of the leaves, more than reach the instruction, so a proven division never traps.
"""

from __future__ import annotations

from typing import Optional

from ..ir.expr import Const, Expr, mask, shape_key, variables
from . import memo
from .verify import _z3_context, to_z3

TRAP_PROOF_MS = 200


def never_traps(op: str, dividend: Expr, divisor: Expr, timeout_ms: int = TRAP_PROOF_MS) -> bool:
    """True if `op` ("udiv", "urem", "sdiv", "srem") cannot trap for any value of its operands' leaves."""
    signed = op in ("sdiv", "srem")
    if isinstance(divisor, Const):
        if divisor.value == 0:
            return False
        if not signed or divisor.value != mask(divisor.width):
            return True
    # Unsigned: only the divisor matters, so dividends of every shape share the verdict.
    key = ("traps", signed, shape_key(dividend, divisor) if signed else shape_key(divisor))
    found = memo.get(key)
    if found is not memo.MISSING:
        return found
    proven = _solve(divisor, dividend if signed else None, signed, timeout_ms)
    if proven is not None:
        memo.put(key, proven)
    return proven is True


def _solve(divisor: Expr, dividend: Optional[Expr], signed: bool, timeout_ms: int) -> Optional[bool]:
    """True if no input traps, False if one does, None if the solver gave up."""
    try:
        import z3
    except ImportError:
        return None
    ctx = _z3_context()
    leaves = variables(divisor) + (variables(dividend) if dividend is not None else [])
    z3_vars = {v.name: z3.BitVec(v.name, v.width, ctx) for v in leaves}
    w = divisor.width
    d = to_z3(divisor, z3_vars, ctx)
    trap = d == z3.BitVecVal(0, w, ctx)
    if signed:
        x = to_z3(dividend, z3_vars, ctx)
        trap = z3.Or(trap, z3.And(x == z3.BitVecVal(1 << (w - 1), w, ctx), d == z3.BitVecVal(mask(w), w, ctx)))
    solver = z3.Solver(ctx=ctx)
    solver.set("timeout", max(1, int(timeout_ms)))
    solver.add(trap)
    result = solver.check()
    if result == z3.unsat:
        return True
    if result == z3.sat:
        return False
    return None
