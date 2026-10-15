"""Binja-independent pipeline: normalize, Tier 1 rules, Tier 2 backends, verify."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .ir.expr import BinOp, Expr, UnOp, is_mba, node_count, to_str, variables
from .ir.normalize import normalize
from .rules.base import Rewrite, Rule
from .semantic.backend import Tier2Backend
from .semantic.verify import Verdict, check_equivalent

_MAX_RULE_REWRITES = 64


@dataclass(frozen=True)
class Limits:
    max_vars: int = 4
    max_nodes: int = 200
    # Tier 2 retries a failing backend on every nested MBA subtree; this bounds the total per expression.
    tier2_seconds: float = 1.0


@dataclass
class Outcome:
    rewrite: Optional[Rewrite] = None
    skipped: Optional[str] = None
    errors: list[tuple[str, str]] = field(default_factory=list)


def simplify_expression(
    expr: Expr,
    rules: Sequence[Rule],
    backends: Sequence[Tier2Backend],
    limits: Limits = Limits(),
    disabled: frozenset[str] = frozenset(),
    check: Callable[[Expr, Expr], Verdict] = check_equivalent,
) -> Outcome:
    out = Outcome()
    size = node_count(expr)
    if size > limits.max_nodes:
        out.skipped = f"too complex: {size} nodes > {limits.max_nodes}"
        return out
    dead = set(disabled)
    sources: list[str] = []
    current = _tier1(normalize(expr), rules, dead, sources, out.errors)
    if is_mba(current) and backends:
        current, exhausted = _tier2(current, backends, limits, dead, sources, out.errors, check)
        if exhausted:
            out.skipped = f"too complex: Tier 2 time budget ({limits.tier2_seconds:g} s) exhausted"
    if out.skipped is None and is_mba(current) and len(variables(current)) > limits.max_vars:
        out.skipped = f"too complex: {len(variables(current))} variables > {limits.max_vars}"
    if not sources or node_count(current) >= size:
        return out
    if check(expr, current) is Verdict.REFUTED:
        out.errors.append(("verify", f"refuted {to_str(expr)} -> {to_str(current)} via {sources}"))
        return out
    out.rewrite = Rewrite(original=expr, replacement=current, source="+".join(dict.fromkeys(sources)))
    return out


def _rebuild(node: Expr, visit: Callable[[Expr], Expr]) -> Expr:
    if isinstance(node, BinOp):
        return normalize(BinOp(node.op, visit(node.left), visit(node.right), node.width))
    if isinstance(node, UnOp):
        return normalize(UnOp(node.op, visit(node.operand), node.width))
    return node


def _tier1(e: Expr, rules: Sequence[Rule], dead: set[str], sources: list[str], errors: list) -> Expr:
    budget = [_MAX_RULE_REWRITES]

    def visit(node: Expr) -> Expr:
        node = _rebuild(node, visit)
        while budget[0] > 0:
            rewrite = _first_rule(node, rules, dead, errors)
            if rewrite is None:
                break
            budget[0] -= 1
            sources.append(rewrite.source)
            node = visit(rewrite.replacement)
        return node

    return visit(e)


def _first_rule(node: Expr, rules: Sequence[Rule], dead: set[str], errors: list) -> Optional[Rewrite]:
    for rule in rules:
        if rule.name in dead:
            continue
        try:
            rewrite = rule.match(node)
        except Exception as exc:
            dead.add(rule.name)
            errors.append((rule.name, f"{type(exc).__name__}: {exc}"))
            continue
        if rewrite is not None and rewrite.replacement != node and node_count(rewrite.replacement) <= node_count(node):
            return rewrite
    return None


def _tier2(e, backends, limits, dead, sources, errors, check) -> tuple[Expr, bool]:
    """Simplify MBA subtrees top-down. Returns (result, whether the time budget ran out)."""
    deadline = time.monotonic() + limits.tier2_seconds
    exhausted = False

    def visit(node: Expr) -> Expr:
        nonlocal exhausted
        if not is_mba(node):
            return node
        if len(variables(node)) <= limits.max_vars:
            for backend in backends:
                source = f"tier2:{backend.name}"
                if source in dead:
                    continue
                if time.monotonic() > deadline:
                    exhausted = True
                    return node
                try:
                    result = backend.simplify(node)
                except Exception as exc:
                    dead.add(source)
                    errors.append((source, f"{type(exc).__name__}: {exc}"))
                    continue
                if result is None or node_count(result) >= node_count(node):
                    continue
                if check(node, result) is Verdict.REFUTED:
                    errors.append((source, f"refuted {to_str(node)} -> {to_str(result)}"))
                    continue
                sources.append(source)
                return result
        return _rebuild(node, visit)

    return visit(e), exhausted
