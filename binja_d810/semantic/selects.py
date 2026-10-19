"""Conditions that read their inputs only through one boolean (v4.2).

A branchless select (x86 neg; sbb; and; add) is c ? A : B over a single condition c. A comparison of it
with a constant is then c, its inverse, or a constant: evaluating it at c = 0 and c = 1 decides which.
The caller proves the result (simplify._finish folds need a PROVEN verdict).
"""

from __future__ import annotations

from typing import Optional

from ..ir.evaluate import evaluate
from ..ir.expr import CMP_OPS, BinOp, Const, Expr, UnOp, Var, variables
from .constancy import sampled_constant

_INVERSE = {"eq": "ne", "ne": "eq", "ult": "uge", "uge": "ult", "ule": "ugt", "ugt": "ule",
            "slt": "sge", "sge": "slt", "sle": "sgt", "sgt": "sle"}


def booleans(e: Expr) -> list[Expr]:
    """The maximal 1-bit sub-expressions that read variables (comparisons and flag variables), distinct,
    in order of first occurrence."""
    found: list[Expr] = []

    def visit(node: Expr) -> None:
        if node.width == 1 and (isinstance(node, Var) or (isinstance(node, BinOp) and node.op in CMP_OPS)) \
                and variables(node):
            if node not in found:
                found.append(node)
            return
        if isinstance(node, BinOp):
            visit(node.left)
            visit(node.right)
        elif isinstance(node, UnOp):
            visit(node.operand)

    visit(e)
    return found


def replace(e: Expr, old: Expr, new: Expr) -> Expr:
    if e == old:
        return new
    if isinstance(e, BinOp):
        return BinOp(e.op, replace(e.left, old, new), replace(e.right, old, new), e.width)
    if isinstance(e, UnOp):
        return UnOp(e.op, replace(e.operand, old, new), e.width)
    return e


def inverse(c: Expr) -> Expr:
    """not c, for a comparison or a 1-bit variable."""
    if isinstance(c, BinOp) and c.op in _INVERSE:
        return BinOp(_INVERSE[c.op], c.left, c.right, 1)
    return BinOp("eq", c, Const(0, 1), 1)


def fold_select(e: Expr) -> Optional[Expr]:
    """c, not c or a constant for a 1-bit `e` that reads its variables only through one boolean c below its
    root; else None. Each arm is evaluated, or sampled when variables cancel (x - x): unproven either way."""
    if e.width != 1 or not isinstance(e, (BinOp, UnOp)):
        return None
    found: list[Expr] = []
    for child in ((e.left, e.right) if isinstance(e, BinOp) else (e.operand,)):
        found += [c for c in booleans(child) if c not in found]
    if len(found) != 1:
        return None
    c = found[0]
    arms = []
    for bit in (0, 1):
        fixed = replace(e, c, Const(bit, 1))
        try:
            value = sampled_constant(fixed) if variables(fixed) else evaluate(fixed, {})
        except ArithmeticError:
            return None
        if value is None:
            return None
        arms.append(value)
    if arms[0] == arms[1]:
        return Const(arms[0], 1)
    return c if arms[1] == 1 else inverse(c)
