"""Is an expression constant? Sample it, then prove the value (spec v3 section 4)."""

from __future__ import annotations

import itertools
from typing import Callable, Optional

from ..deadline import Deadline
from ..ir.evaluate import evaluate
from ..ir.expr import Const, Expr, variables
from .verify import Check, Verdict, check, sample_envs

_EXHAUSTIVE_BITS = 16
CONSTANCY_Z3_MS = 2000

Prover = Callable[[Expr, Expr], Check]


def sampled_constant(e: Expr, samples: int = 256) -> Optional[int]:
    """The value `e` takes on every sample (edge values, then random), or None as soon as two
    samples differ: ordinary expressions are rejected after a sample or two."""
    vs = variables(e)
    if not vs:
        return None  # normalization already folds variable-free expressions
    value = None
    for env in sample_envs({v.name: v.width for v in vs}, samples, seed=11):
        found = evaluate(e, env)
        if value is None:
            value = found
        elif found != value:
            return None
    return value


def constant_value(e: Expr, deadline: Deadline, prove: Optional[Prover] = None) -> Optional[tuple[int, Check]]:
    """(value, proof) if `e` is constant on every sample; the proof's verdict is PROVEN or TESTED.
    None if a sample, an exhaustive check or a proof shows two values."""
    value = sampled_constant(e)
    if value is None:
        return None
    widths = {v.name: v.width for v in variables(e)}
    if sum(widths.values()) <= _EXHAUSTIVE_BITS:
        names = sorted(widths)
        for combo in itertools.product(*(range(1 << widths[n]) for n in names)):
            if evaluate(e, dict(zip(names, combo))) != value:
                return None
        return value, Check(Verdict.PROVEN, "exhaustive")
    if prove is None:
        prove = lambda a, b: check(a, b, deadline=deadline, z3_timeout_ms=CONSTANCY_Z3_MS)  # noqa: E731
    found = prove(e, Const(value, e.width))
    if found.verdict is Verdict.REFUTED:
        return None
    return value, found
