"""Offline per-width proofs of d810 rules (scripts/verify_rules.py; tests). Needs z3.

d810-ng's verify_rule proves a rule's pattern and replacement at the requested width but converts
the rule's constraints with a 32-bit visitor (backends/z3.py, _constraint_expr_to_z3), so at other
widths a constraint such as c == -2 does not apply and identities that hold at every width are
"refuted". This prover uses d810's own expression visitor at the requested width for the pattern,
the replacement and every constraint, adds the rule's custom Z3 constraints, defines dynamic
constants from their compute functions evaluated on Z3 terms, and requires shift counts that are
pattern constants to be below the width (the runtime guard in d810_adapter). A constraint it
cannot translate makes the result "unresolved", never "proven".
"""

from __future__ import annotations

from typing import Any


class _Z3Binding:
    """What a DynamicConst's compute function reads as ctx[name]: .value, .size."""

    def __init__(self, term, width: int):
        self.value = term
        self.size = width // 8


def prove_rule(rule: Any, width: int, *, timeout_ms: int = 60_000, tactic: str | None = None) -> str:
    """"proven", "refuted: <model>" or "unresolved: <reason>"."""
    import z3

    from ..third_party.d810.mba.backends.z3 import (
        Z3VerificationVisitor, _collect_constraint_names, _collect_symbolic_names,
    )
    from ..third_party.d810.mba.constraints import ConstraintExpr
    from ..third_party.d810.mba.dsl import SymbolicExpressionProtocol

    pattern, replacement = rule.pattern, rule.replacement
    if not isinstance(pattern, SymbolicExpressionProtocol) or not isinstance(replacement, SymbolicExpressionProtocol):
        return "unresolved: pattern or replacement is not symbolic"
    constraints = list(getattr(rule, "CONSTRAINTS", None) or [])
    names: set = set()
    _collect_symbolic_names(pattern, names)
    _collect_symbolic_names(replacement, names)
    for constraint in constraints:
        if isinstance(constraint, ConstraintExpr):
            _collect_constraint_names(constraint, names)
    dynamic = dict(getattr(rule, "DYNAMIC_CONSTS", None) or {})
    names |= set(dynamic)
    z3_vars = {name: z3.BitVec(name, width) for name in sorted(names)}
    visitor = Z3VerificationVisitor(bit_width=width, var_map=z3_vars)

    facts = []
    for constraint in constraints:
        if not isinstance(constraint, ConstraintExpr):
            return f"unresolved: constraint {constraint!r} is not declarative"
        try:
            facts.append(_constraint(constraint, visitor, z3))
        except Exception as exc:
            return f"unresolved: constraint {constraint}: {type(exc).__name__}: {exc}"
    try:
        facts += list(rule.get_constraints(z3_vars))
    except Exception as exc:
        return f"unresolved: get_constraints: {type(exc).__name__}: {exc}"
    ctx = {name: _Z3Binding(term, width) for name, term in z3_vars.items()}
    for name, compute in dynamic.items():
        try:
            facts.append(z3_vars[name] == compute(ctx))
        except Exception as exc:
            return f"unresolved: dynamic constant {name}: {type(exc).__name__}: {exc}"
    try:
        lhs, rhs = visitor.visit(pattern), visitor.visit(replacement)
        facts += _shift_guards(pattern, z3_vars, width, z3) + _shift_guards(replacement, z3_vars, width, z3)
    except Exception as exc:
        return f"unresolved: {type(exc).__name__}: {exc}"

    solver = (z3.Then("simplify", "bit-blast", "sat").solver() if tactic == "bitblast" else z3.Solver())
    solver.set("timeout", timeout_ms)
    solver.add(*facts)
    solver.add(lhs != rhs)
    result = solver.check()
    if result == z3.unsat:
        return "proven"
    if result == z3.sat:
        model = solver.model()
        shown = {str(d): model[d].as_long() for d in model.decls() if hasattr(model[d], "as_long")}
        return f"refuted: {shown}"
    return f"unresolved: {solver.reason_unknown()}"


def _constraint(constraint, visitor, z3):
    kind = type(constraint).__name__
    if kind == "EqualityConstraint":
        return visitor.visit(constraint.left) == visitor.visit(constraint.right)
    if kind == "ComparisonConstraint":
        left, right = visitor.visit(constraint.left), visitor.visit(constraint.right)
        return {"ne": lambda: left != right, "lt": lambda: z3.ULT(left, right), "gt": lambda: z3.UGT(left, right),
                "le": lambda: z3.ULE(left, right), "ge": lambda: z3.UGE(left, right)}[constraint.op_name]()
    if kind == "AndConstraint":
        return z3.And(_constraint(constraint.left, visitor, z3), _constraint(constraint.right, visitor, z3))
    if kind == "OrConstraint":
        return z3.Or(_constraint(constraint.left, visitor, z3), _constraint(constraint.right, visitor, z3))
    if kind == "NotConstraint":
        return z3.Not(_constraint(constraint.operand, visitor, z3))
    raise ValueError(f"unknown constraint kind {kind}")


def _shift_guards(sym, z3_vars, width, z3) -> list:
    """Pattern-constant shift counts below the width: the domain where Z3's shifts and Binary
    Ninja's (count modulo max(32, width)) agree, and the only one where the adapter fires."""
    guards, stack = [], [sym]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if getattr(node, "operation", None) in ("shl", "shr", "sar"):
            count = node.right
            if getattr(count, "operation", 1) is None and getattr(count, "value", None) is None:
                name = getattr(count, "name", None)
                if name in z3_vars:
                    guards.append(z3.ULT(z3_vars[name], z3.BitVecVal(width, width)))
        stack.extend([getattr(node, "left", None), getattr(node, "right", None)])
    return guards
