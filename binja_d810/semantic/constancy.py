"""Is an expression constant? Sample it, then prove the value."""

from __future__ import annotations

import itertools
import random
from typing import Callable, Iterator, Optional

from ..deadline import Deadline
from ..ir.evaluate import evaluate, evaluate_shared, evaluate_shared_batch
from ..ir.expr import Const, Expr, mask, shape_key, variables
from . import memo
from .low_bits import low_bits_value
from .verify import Check, Verdict, _z3_prove, check, sample_envs

_EXHAUSTIVE_BITS = 16
CONSTANCY_Z3_MS = 2000
_SHARED_SAMPLES = 32  # after the 21 joint and small-value samples

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


def fold_shared(e: Expr, deadline: Deadline, z3_timeout_ms: int = CONSTANCY_Z3_MS,
                inconclusive: Optional[dict] = None) -> Optional[tuple[int, Check]]:
    """constant_value for an expression too large to expand as a tree (values shared: an OLLVM-obfuscated
    export, spec v3.1 goal 1). Samples evaluate each shared value once and the Z3 query is built
    with sharing. (value, check) when constant on every sample (or variable-free); the check says
    whether it is proven. None as soon as two samples differ, or when a proof refutes it. Proven and refuted outcomes are
    remembered by shape. `inconclusive` holds one run's inconclusive outcomes by shape (v3.5): a proof
    that ran out of its own time is not tried again in that run, though another moment may decide it; one
    shortened by the deadline is not kept."""
    key = ("fold", shape_key(e))
    found = memo.get(key)
    if found is not memo.MISSING:
        return found
    if inconclusive is not None and key in inconclusive:
        return inconclusive[key]
    full = deadline.at == float("inf") or deadline.remaining() * 1000 >= z3_timeout_ms
    result = _fold_shared(e, deadline, z3_timeout_ms)
    if result is None or result[1].verdict is Verdict.PROVEN:
        memo.put(key, result)
    elif inconclusive is not None and full:
        inconclusive[key] = result
    return result


def _fold_shared(e: Expr, deadline: Deadline, z3_timeout_ms: int) -> Optional[tuple[int, Check]]:
    widths = {v.name: v.width for v in variables(e)}
    if not widths:
        # Exact (a per-path variant whose merged values were constants, spec v3.2 goal 3).
        return evaluate_shared(e, {}), Check(Verdict.PROVEN, "evaluation")
    # A few dozen samples: they guess the value and reject ordinary roots; the proof decides (v3.3:
    # 277 evaluations of each shared value took most of a large function's budget). Two first, then the
    # rest in one batch.
    envs = list(itertools.chain(_joint_envs(widths), sample_envs(widths, _SHARED_SAMPLES, seed=11)))
    first = evaluate_shared_batch(e, envs[:2])
    if first[0] != first[1] or any(found != first[0] for found in evaluate_shared_batch(e, envs[2:])):
        return None
    value = first[0]
    decided = low_bits_value(e)
    if decided is not None:  # a polynomial predicate: no solver needed
        return (value, Check(Verdict.PROVEN, "low bits")) if decided == value else None
    if deadline.at != float("inf"):
        remaining_ms = deadline.remaining() * 1000
        if remaining_ms < 2:
            return value, Check(Verdict.TESTED, "sampling", "proof budget exhausted")
        z3_timeout_ms = min(z3_timeout_ms, int(remaining_ms))
    proven, reason = _z3_prove(e, Const(value, e.width), widths, z3_timeout_ms)
    if proven is False:
        return None
    return value, Check(Verdict.PROVEN, "z3") if proven else Check(Verdict.TESTED, "sampling", reason)
