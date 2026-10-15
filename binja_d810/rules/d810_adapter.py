"""Adapts a vendored d810 VerifiableRule to the Rule protocol."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ..ir.expr import BinOp, Const, Expr, UnOp, walk
from ..ir.normalize import normalize
from ..matching.matcher import D810_BINARY, D810_UNARY, match
from ..third_party.d810.mba.constraints import ConstraintExpr
from ..third_party.d810.mba.dsl import DynamicConst
from .base import Rewrite
from .d810_ops import unsupported_ops

_CONSTRAINT_ERRORS = (KeyError, ValueError, AttributeError, TypeError, ArithmeticError)


class Binding:
    """What d810's constraint code sees as candidate[name]: .value, .mop, .size."""

    __slots__ = ("expr",)

    def __init__(self, expr: Expr):
        self.expr = expr

    @property
    def value(self) -> int:
        if isinstance(self.expr, Const):
            return self.expr.value
        raise AttributeError("non-constant binding has no value")

    @property
    def mop(self) -> Expr:
        return self.expr

    @property
    def size(self) -> int:
        return self.expr.width // 8


class D810Rule:
    def __init__(self, rule: Any, widths: Iterable[int]):
        self.name = type(rule).__name__
        # The public properties, not cls._dsl_*: those are absent when a side is not symbolic.
        self.pattern = rule.pattern
        self.replacement = rule.replacement
        self.constraints = list(getattr(rule, "CONSTRAINTS", None) or [])
        self.dynamic_consts = dict(getattr(rule, "DYNAMIC_CONSTS", None) or {})
        self.widths = frozenset(widths)
        bad = unsupported_ops(self.pattern) | unsupported_ops(self.replacement)
        if bad:
            raise ValueError(f"{self.name}: unsupported d810 ops {sorted(bad)}")

    def match(self, e: Expr) -> Optional[Rewrite]:
        if e.width not in self.widths:
            return None
        for bindings in match(self.pattern, e):
            ctx = self._context(bindings, e.width)
            if ctx is None:
                continue
            try:
                replacement = build(self.replacement, ctx, e.width)
            except _CONSTRAINT_ERRORS:
                continue
            if _shifts_out_of_range(e) or _shifts_out_of_range(replacement):
                continue
            return Rewrite(original=e, replacement=normalize(replacement), source=self.name)
        return None

    def _context(self, bindings: dict[str, Expr], width: int) -> Optional[dict[str, Any]]:
        ctx: dict[str, Any] = {name: Binding(expr) for name, expr in bindings.items()}
        ctx["_width"] = width
        changed = True
        while changed:  # defining constraints, e.g. c_res == c_1 + ONE
            changed = False
            for constraint in self.constraints:
                if not isinstance(constraint, ConstraintExpr):
                    continue
                try:
                    name, value = constraint.eval_and_define(ctx)
                except _CONSTRAINT_ERRORS:
                    continue
                if name is not None and name not in ctx:
                    ctx[name] = Binding(Const(value, width))
                    changed = True
        for name, compute in self.dynamic_consts.items():
            try:
                ctx[name] = Binding(Const(compute(ctx), width))
            except _CONSTRAINT_ERRORS:
                return None
        for constraint in self.constraints:
            try:
                if not constraint(ctx):
                    return None
            except _CONSTRAINT_ERRORS:
                return None
        return ctx


def _natural_width(sym: Any, ctx: dict[str, Any]) -> Optional[int]:
    stack = [sym]
    while stack:
        node = stack.pop()
        if isinstance(node, DynamicConst):
            continue
        if node.operation is None:
            binding = ctx.get(node.name)
            if isinstance(binding, Binding) and not isinstance(binding.expr, Const):
                return binding.expr.width
            continue
        stack.extend(child for child in (node.left, node.right) if child is not None)
    return None


def build(sym: Any, ctx: dict[str, Any], width: int) -> Expr:
    if isinstance(sym, DynamicConst):
        return Const(sym.compute(ctx), width)
    op = sym.operation
    if op is None:
        binding = ctx.get(sym.name)
        if isinstance(binding, Binding):
            expr = binding.expr
            return Const(expr.value, width) if isinstance(expr, Const) else expr
        if sym.value is not None:
            return Const(sym.value, width)
        raise KeyError(sym.name)
    if op == "zext":
        inner_width = _natural_width(sym.left, ctx)
        if inner_width is None:
            raise ValueError("cannot infer zext operand width")
        return UnOp("zx", build(sym.left, ctx, inner_width), sym.value)
    if op in D810_UNARY:
        return UnOp(D810_UNARY[op], build(sym.left, ctx, width), width)
    return BinOp(D810_BINARY[op], build(sym.left, ctx, width), build(sym.right, ctx, width), width)


def _shifts_out_of_range(e: Expr) -> bool:
    """A constant shift count at or above the width. d810 proved its rules with Z3's shifts, which
    never reduce the count; Binary Ninja's IL reduces it modulo max(32, width) (ir.evaluate). The two
    agree only below the width, so rules never fire outside it (CstSimplificationRule7/15/19)."""
    return any(isinstance(n, BinOp) and n.op in ("shl", "lshr", "ashr") and isinstance(n.right, Const)
               and n.right.value >= n.width for n in walk(e))
