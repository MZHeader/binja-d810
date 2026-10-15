"""Tier 2 backend that delegates to CoBRA (Trail of Bits) through cobra_shim."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable, Optional

from ..ir.expr import BinOp, Const, Expr, UnOp, Var, substitute, to_str, variables
from ..ir.normalize import normalize
from ..ir.parse import ParseError, parse

_MISS = object()
_SOLVER_BINARY = frozenset({"add", "sub", "mul", "and", "or", "xor"})
_SOLVER_UNARY = frozenset({"neg", "not"})


def _atomize(e: Expr, width: int, atoms: dict[Expr, Var]) -> Expr:
    """Replace what CoBRA cannot take (shifts, extensions, narrow reads of wider values) by fresh
    variables; equal subterms share one. An identity that holds for every value of such an atom
    also holds for the subterm, and every result is verified afterwards anyway."""
    if isinstance(e, Const) or (isinstance(e, Var) and e.width == width):
        return e
    if isinstance(e, UnOp) and e.op in _SOLVER_UNARY:
        return UnOp(e.op, _atomize(e.operand, width, atoms), width)
    if isinstance(e, BinOp) and e.op in _SOLVER_BINARY:
        return BinOp(e.op, _atomize(e.left, width, atoms), _atomize(e.right, width, atoms), width)
    if e not in atoms:
        atoms[e] = Var(f"atom@{len(atoms)}", width)
    return atoms[e]


class CobraBackend:
    name = "cobra"

    def __init__(self, solve: Callable[[str, int], Optional[str]], cache_size: int = 4096):
        self._solve = solve
        # Tier 2 meets the same subexpressions again in every candidate of a chain and in every
        # fixpoint iteration; CoBRA's answer to an identical (renamed) query is reused.
        self._cache: "OrderedDict[tuple[str, int], Optional[str]]" = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def simplify(self, e: Expr, deadline=None) -> Optional[Expr]:
        atoms: dict[Expr, Var] = {}
        solvable = _atomize(e, e.width, atoms)
        vs = variables(solvable)
        plain = {v.name: Var(f"v{i}", e.width) for i, v in enumerate(vs)}
        query = (to_str(substitute(solvable, plain)), e.width)
        with self._lock:
            cached = self._cache.get(query, _MISS)
        if cached is _MISS:
            text = self._solve(*query) if deadline is None else self._solve(*query, deadline)
            if text is not None or deadline is None or not deadline.expired():  # a cut-off None is not an answer
                with self._lock:
                    self._cache[query] = text
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
        else:
            text = cached
        if text is None:
            return None
        try:
            parsed = parse(text, e.width)
        except ParseError:
            return None
        restore = {atom.name: term for term, atom in atoms.items()}
        back = {f"v{i}": restore.get(v.name, v) for i, v in enumerate(vs)}
        if any(v.name not in back for v in variables(parsed)):
            return None
        return normalize(substitute(parsed, back))
