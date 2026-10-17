"""Turn a d810 DSL pattern into a concrete IR expression (tests and the sample generator)."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from ..ir.expr import BinOp, Const, Expr, UnOp, Var
from ..matching.matcher import D810_BINARY, D810_UNARY

DEFAULT_NAMES = {"x_0": "x", "x_1": "y", "x_2": "z"}


class _Value:
    """What a DynamicConst computation reads as ctx[name].value."""

    def __init__(self, value: int):
        self.value = value


def instantiate(sym: Any, width: int, names: Optional[Mapping[str, str]] = None,
                constants: Optional[Mapping[str, int]] = None) -> Optional[Expr]:
    """Var leaves become IR Vars (renamed via `names`); `bnot_<v>` leaves become ~<v>.

    Symbolic constants take their value from `constants` (binja_d810.rules.prove.solve_constants);
    without one the result is None, as it is for zext.
    """
    names = DEFAULT_NAMES if names is None else names
    if type(sym).__name__ == "DynamicConst":  # a constant computed from the match (PredOdd1: 0)
        ctx = {name: _Value(value) for name, value in (constants or {}).items()}
        ctx["size"] = width // 8
        try:
            return Const(sym.compute(ctx), width)
        except (KeyError, AttributeError, TypeError, ValueError):
            return None
    op = getattr(sym, "operation", None)
    if op == "bool_to_int":  # (l op r).to_int(): zx(cmp), or the bare comparison at width 1
        from ..matching.matcher import comparison_of

        kind = comparison_of(sym.constraint)
        operand = 32 if width == 1 else width
        left = instantiate(sym.constraint.left, operand, names, constants)
        right = instantiate(sym.constraint.right, operand, names, constants)
        if kind is None or left is None or right is None:
            return None
        cmp = BinOp(kind, left, right, 1)
        return cmp if width == 1 else UnOp("zx", cmp, width)
    if op is None:
        if getattr(sym, "value", None) is not None:
            return Const(sym.value, width)
        if constants is not None and getattr(sym, "name", None) in constants:
            return Const(constants[sym.name], width)
        if getattr(sym, "is_pattern_constant", False) or not hasattr(sym, "name"):
            return None
        if sym.name.startswith("bnot_"):
            base = sym.name.removeprefix("bnot_")
            base = re.sub(r"^x(\d+)$", r"x_\1", base)  # some rules spell bnot_x_2 as bnot_x2
            return UnOp("not", Var(names.get(base, base), width), width)
        return Var(names.get(sym.name, sym.name), width)
    if op in D810_UNARY:
        inner = instantiate(sym.left, width, names, constants)
        return None if inner is None else UnOp(D810_UNARY[op], inner, width)
    if op in D810_BINARY:
        left, right = instantiate(sym.left, width, names, constants), instantiate(sym.right, width, names, constants)
        return None if left is None or right is None else BinOp(D810_BINARY[op], left, right, width)
    return None
