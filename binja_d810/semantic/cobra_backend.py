"""Tier 2 backend that delegates to CoBRA (Trail of Bits) through cobra_shim."""

from __future__ import annotations

from typing import Callable, Optional

from ..ir.expr import BinOp, Expr, UnOp, Var, substitute, to_str, variables, walk
from ..ir.normalize import normalize
from ..ir.parse import ParseError, parse

_UNSUPPORTED = frozenset({"lshr", "ashr", "zx", "sx", "trunc"})


class CobraBackend:
    name = "cobra"

    def __init__(self, solve: Callable[[str, int], Optional[str]]):
        self._solve = solve

    def simplify(self, e: Expr) -> Optional[Expr]:
        if any(isinstance(n, (BinOp, UnOp)) and n.op in _UNSUPPORTED for n in walk(e)):
            return None
        vs = variables(e)
        if any(v.width != e.width for v in vs):
            return None
        plain = {v.name: Var(f"v{i}", e.width) for i, v in enumerate(vs)}
        text = self._solve(to_str(substitute(e, plain)), e.width)
        if text is None:
            return None
        try:
            parsed = parse(text, e.width)
        except ParseError:
            return None
        back = {f"v{i}": v for i, v in enumerate(vs)}
        if any(v.name not in back for v in variables(parsed)):
            return None
        return normalize(substitute(parsed, back))
