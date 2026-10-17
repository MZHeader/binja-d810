"""Is an expression constant? Sample it, then prove the value (spec v3 section 4)."""

from __future__ import annotations

import itertools
import random
from typing import Callable, Iterator, Optional

from ..deadline import Deadline
from ..ir.evaluate import evaluate
from ..ir.expr import Const, Expr, mask, variables
from .verify import Check, Verdict, check, sample_envs

_EXHAUSTIVE_BITS = 16
CONSTANCY_Z3_MS = 2000

Prover = Callable[[Expr, Expr], Check]


def _joint_envs(widths: dict[str, int]) -> Iterator[dict[str, int]]:
    """Every variable at the same edge value, then small values: a comparison with a small value
    (((i ^ x) & 7) == y) or a joint edge ((x & y) == -1) changes there, and random wide samples
    almost never reach it."""
    for pick in (lambda w: 0, lambda w: 1, lambda w: 2, lambda w: mask(w), lambda w: 1 << (w - 1)):
        yield {name: pick(w) & mask(w) for name, w in widths.items()}
    rng = random.Random(5)
    for _ in range(16):
        yield {name: rng.getrandbits(min(w, 3)) for name, w in widths.items()}


def sampled_constant(e: Expr, samples: int = 256) -> Optional[int]:
    """The value `e` takes on every sample (joint and small values, edge values, then random), or
    None as soon as two samples differ: ordinary expressions are rejected after a sample or two."""
    vs = variables(e)
    if not vs:
        return None  # normalization already folds variable-free expressions
    widths = {v.name: v.width for v in vs}
    value = None
    for env in itertools.chain(_joint_envs(widths), sample_envs(widths, samples, seed=11)):
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
