"""Comparisons decided by the low bits of their inputs: the classic polynomial opaque predicates.

Addition, subtraction, multiplication, negation, the bitwise operations, shifts left by a constant
and extensions all commute with truncation: the low k bits of the result depend only on the low k
bits of the inputs (an operation narrower than k bits on the inputs' low bits as far as its width).
So if no assignment of the inputs' low k bits makes the two sides of an equality agree there, no
assignment of the inputs makes them equal: 7 * y * y - 1 is 3, 6 or 7 modulo 8, and x * x is 0, 1
or 4. Z3 is slow on such nonlinear products; this is exact and needs at most 4096 evaluations.

Any other subterm (a division, a right shift) is an atom: a value of its own, over what its known
bits and bound allow (2 / (y * y + 1) is 0, 1 or 2). Treating it as independent of the other inputs
only adds assignments, so a proof stays a proof.
"""

from __future__ import annotations

import itertools
from typing import Optional

from ..ir.bits import possible_ones, upper_bound
from ..ir.evaluate import evaluate_shared_batch
from ..ir.expr import BinOp, Const, Expr, UnOp, Var, distinct_nodes, mask, nodes, variables

_MAX_NAMES = 4
_MAX_K = 4
_MAX_CASES = 4096
_MAX_NODES = 64
_SMALL_BOUND = 16  # an atom bounded below this takes its exact values
_KEPT_BINARY = frozenset({"add", "sub", "mul", "and", "or", "xor"})
_KEPT_UNARY = frozenset({"neg", "not", "zx", "sx", "trunc"})


def _nonlinear(e: Expr) -> bool:
    return any(isinstance(n, BinOp) and n.op == "mul" and not isinstance(n.left, Const)
               and not isinstance(n.right, Const) for n in nodes(e))


def _abstract(e: Expr, atoms: dict[Expr, Var], taken: set[str], memo: dict[int, Expr]) -> Expr:
    """`e` with every maximal subterm that does not commute with truncation replaced by a variable."""
    found = memo.get(id(e))
    if found is not None:
        return found
    if isinstance(e, (Var, Const)):
        found = e
    elif isinstance(e, UnOp) and e.op in _KEPT_UNARY:
        found = UnOp(e.op, _abstract(e.operand, atoms, taken, memo), e.width)
    elif isinstance(e, BinOp) and (e.op in _KEPT_BINARY or (e.op == "shl" and isinstance(e.right, Const))):
        found = BinOp(e.op, _abstract(e.left, atoms, taken, memo), _abstract(e.right, atoms, taken, memo), e.width)
    else:
        if e not in atoms:
            name = f"atom@{len(atoms)}"
            while name in taken:
                name += "_"
            taken.add(name)
            atoms[e] = Var(name, e.width)
        found = atoms[e]
    memo[id(e)] = found
    return found


def _domain(width: int, k: int, possible: int, bound: Optional[int]) -> list[int]:
    """The values to try for one input: its exact values when bounded below _SMALL_BOUND, else every
    pattern of its low k bits (no wider than the input) its possible ones allow."""
    if bound is not None and bound < _SMALL_BOUND:
        return [v for v in range(bound + 1) if v & ~possible == 0]
    return [v for v in range(1 << min(k, width)) if v & ~possible == 0]


def low_bits_value(e: Expr) -> Optional[int]:
    """The value (0 or 1) of an eq/ne comparison over a nonlinear product when its inputs' low bits
    decide it; None otherwise."""
    if not isinstance(e, BinOp) or e.op not in ("eq", "ne"):
        return None
    if distinct_nodes(e) > _MAX_NODES or not _nonlinear(e):
        return None
    atoms: dict[Expr, Var] = {}
    taken = {v.name for v in variables(e)}
    memo: dict[int, Expr] = {}
    width = e.left.width
    difference = BinOp("sub", _abstract(e.left, atoms, taken, memo), _abstract(e.right, atoms, taken, memo), width)
    names = sorted(variables(difference), key=lambda v: v.name)
    if len(names) > _MAX_NAMES:
        return None
    bounds = {atom.name: (possible_ones(term), upper_bound(term)) for term, atom in atoms.items()}
    within = possible_ones(e.left) | possible_ones(e.right)
    for k in range(1, min(_MAX_K, width) + 1):
        domains = [_domain(v.width, k, *bounds.get(v.name, (mask(v.width), None))) for v in names]
        cases = 1
        for domain in domains:
            cases *= len(domain)
        if cases > _MAX_CASES:
            return None
        envs = [dict(zip((v.name for v in names), values)) for values in itertools.product(*domains)]
        low = mask(k)
        agree = [d & low == 0 for d in evaluate_shared_batch(difference, envs)]
        if not any(agree):
            return int(e.op == "ne")  # never equal
        if all(agree) and within <= low:
            return int(e.op == "eq")  # both sides fit in k bits: always equal
    return None
