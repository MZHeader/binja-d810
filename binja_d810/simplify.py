"""Binja-independent pipeline: normalize, Tier 1 rules, Tier 2 backends, verify."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Union

from .ir.expr import BinOp, Const, Expr, UnOp, is_mba, node_count, relink, to_str, variables
from .ir.normalize import normalize
from .rules.base import Rewrite, Rule
from .semantic.backend import Tier2Backend
from .semantic.linear_mba import is_linear_mba
from .deadline import Deadline
from .semantic.verify import Check, Verdict
from .semantic.verify import check as verify_check

_MAX_RULE_REWRITES = 64


@dataclass(frozen=True)
class Limits:
    max_vars: int = 4
    max_nodes: int = 200
    # Tier 2 retries a failing backend on every nested MBA subtree; this bounds the total per expression.
    # CoBRA's own limit is 1 s, and polynomial MBA needs most of it; the function's shared budget
    # (binja_d810.functionSeconds) bounds the total.
    tier2_seconds: float = 2.0


@dataclass
class Outcome:
    rewrite: Optional[Rewrite] = None
    skipped: Optional[str] = None
    errors: list[tuple[str, str]] = field(default_factory=list)
    refuted: int = 0  # candidate results the checker refuted (never applied)


Checker = Callable[[Expr, Expr], Union[Check, Verdict]]


def _checked(checker: Checker, a: Expr, b: Expr) -> Check:
    result = checker(a, b)
    return result if isinstance(result, Check) else Check(result, "custom")


def simplify_expression(
    expr: Expr,
    rules: Sequence[Rule],
    backends: Sequence[Tier2Backend],
    limits: Limits = Limits(),
    disabled: frozenset[str] = frozenset(),
    check: Optional[Checker] = None,
    deadline: Deadline = Deadline.never(),
    tier1_cache: Optional[dict] = None,
) -> Outcome:
    """`deadline` is the function's shared allowance: once it passes, no new rule, backend or
    proof work starts. `check` replaces the verifier (tests); by default it honours the deadline.
    `tier1_cache` (shared by the calls of one function's run) reuses Tier 1 results: the engine
    asks again about candidates that cheap backends did not simplify, and in later passes."""
    out = Outcome()
    step_check = check
    if check is None:
        check = lambda a, b: verify_check(a, b, deadline=deadline)  # noqa: E731
        # Each Tier 2 step is checked without Z3 (sampling, signatures): a wrong step is rejected
        # there almost always, and the final check of original against result is the proof.
        step_check = lambda a, b: verify_check(a, b, use_z3=False)  # noqa: E731
    if deadline.expired():
        out.skipped = "budget exhausted"
        return out
    size = node_count(expr)
    if size > limits.max_nodes:
        out.skipped = f"too complex: {size} nodes > {limits.max_nodes}"
        return out
    dead = set(disabled)
    sources: list[str] = []
    start = normalize(expr)
    key = (start, frozenset(dead))
    cached = tier1_cache.get(key) if tier1_cache is not None else None
    if cached is not None:
        current, found = cached
        current = relink(current, start)  # the cached result may carry another expression's refs
        sources.extend(found)
    else:
        current = _tier1(start, rules, dead, sources, out.errors, deadline)
        if tier1_cache is not None and not deadline.expired():
            tier1_cache[key] = (current, tuple(sources))
    if is_mba(current) and backends and not deadline.expired():
        current, exhausted = _tier2(current, backends, limits, dead, sources, out, step_check, deadline)
        if exhausted:
            out.skipped = ("budget exhausted" if deadline.expired()
                           else f"too complex: Tier 2 time budget ({limits.tier2_seconds:g} s) exhausted")
    elif is_mba(current) and backends:
        out.skipped = "budget exhausted"
    if out.skipped is None and is_mba(current) and len(variables(current)) > limits.max_vars:
        out.skipped = f"too complex: {len(variables(current))} variables > {limits.max_vars}"
    if not sources:
        if not _cancelled_by_normalization(current, expr):
            return out
        sources.append("normalize")  # its own gate: the mix is gone and the size at least halved
    elif not _worth_applying(current, expr):
        return out
    final = _checked(check, expr, current)
    if final.verdict is Verdict.REFUTED:
        out.refuted += 1
        out.errors.append(("verify", f"refuted {to_str(expr)} -> {to_str(current)} via {sources}"))
        return out
    out.rewrite = Rewrite(original=expr, replacement=current, source="+".join(dict.fromkeys(sources)),
                          verdict=final.verdict, method=final.method, reason=final.reason)
    return out


def _better(new: Expr, old: Expr) -> bool:
    """Smaller: fewer nodes, or as many nodes without mixing arithmetic and bitwise operations."""
    n, o = node_count(new), node_count(old)
    return n < o or (n == o and is_mba(old) and not is_mba(new))


_MIN_SHRINK = 0.75


def _worth_applying(new: Expr, original: Expr) -> bool:
    """Final gate: remove the arithmetic/bitwise mix, or shrink the normalized input by >= 25%.

    Obfuscation inflates expressions far more than that; small trims are churn on ordinary code
    (CoBRA turns CRC's 0xedb88320 & -(crc & 1) into 0xedb88320 * (crc & 1), 6 -> 5 nodes).
    """
    base = normalize(original)
    if is_mba(base) and not is_mba(new):
        return True
    return node_count(new) <= _MIN_SHRINK * node_count(base)


def _cancelled_by_normalization(new: Expr, original: Expr) -> bool:
    """Normalization alone is not a rewrite (canonical clean-ups like x + ~~y are left to Binary
    Ninja), unless it removes the arithmetic/bitwise mix and at least halves the expression:
    (x | z) + (y | z) - ((z | x) + (z | y)) cancels to 0 once both sums are canonical."""
    return is_mba(original) and not is_mba(new) and node_count(new) * 2 <= node_count(original)


def _rebuild(node: Expr, visit: Callable[[Expr], Expr]) -> Expr:
    if isinstance(node, BinOp):
        return normalize(BinOp(node.op, visit(node.left), visit(node.right), node.width))
    if isinstance(node, UnOp):
        return normalize(UnOp(node.op, visit(node.operand), node.width))
    return node


def _tier1(e: Expr, rules: Sequence[Rule], dead: set[str], sources: list[str], errors: list,
           deadline: Deadline = Deadline.never()) -> Expr:
    budget = [_MAX_RULE_REWRITES]
    index = RuleIndex(rules)

    def visit(node: Expr, chain: frozenset[Expr]) -> Expr:
        """`chain` holds what this position was rewritten from: rules can undo each other."""
        node = _rebuild(node, lambda child: visit(child, frozenset()))
        chain = chain | {node}
        while budget[0] > 0 and not deadline.expired():
            rewrite = _best_rule(node, index.candidates(node), dead, errors, chain)
            if rewrite is None:
                break
            budget[0] -= 1
            sources.append(rewrite.source)
            node = visit(rewrite.replacement, chain)
            chain = chain | {node}
        return node

    return visit(e, frozenset())


# Which target node kinds a d810 pattern root can match, through the matcher's equivalences
# (matching/matcher.py): sub <-> add, xor with all-ones <-> not, mul by -1 and 0 - x <-> neg,
# shl by a constant <-> mul, and unary patterns against folded constants.
_TARGETS_OF_ROOT = {
    "add": {"add", "sub"}, "sub": {"sub", "add", "neg"}, "mul": {"mul", "neg"}, "and": {"and"}, "or": {"or"},
    "xor": {"xor", "not"}, "shl": {"shl", "mul"}, "shr": {"lshr"}, "sar": {"ashr"},
    "bnot": {"not", "const"}, "neg": {"neg", "const"}, "zext": {"zx"},
}


def _kind(e: Expr) -> str:
    if isinstance(e, (BinOp, UnOp)):
        return e.op
    return "const" if isinstance(e, Const) else "var"


class RuleIndex:
    """Tier 1 rules by the target node kinds their pattern root can match (v2.1: the full scan
    tried all 156 rules at every node). Rules without a d810 pattern are always candidates."""

    def __init__(self, rules: Sequence[Rule]):
        self.rules = list(rules)
        self._always: list[Rule] = []
        self._by_kind: dict[str, list[Rule]] = {}
        for rule in self.rules:
            root = getattr(getattr(rule, "pattern", None), "operation", "?")
            kinds = _TARGETS_OF_ROOT.get(root)
            if kinds is None:
                self._always.append(rule)
                continue
            for kind in kinds:
                self._by_kind.setdefault(kind, []).append(rule)
        order = {id(rule): i for i, rule in enumerate(self.rules)}
        for kind, bucket in self._by_kind.items():
            self._by_kind[kind] = sorted(bucket + self._always, key=lambda r: order[id(r)])

    def candidates(self, node: Expr) -> list[Rule]:
        return self._by_kind.get(_kind(node), self._always)


def _best_rule(node: Expr, rules: Sequence[Rule], dead: set[str], errors: list, chain: frozenset[Expr]) -> Optional[Rewrite]:
    """The best non-growing rewrite to something not already in `chain`.

    A result that no longer mixes arithmetic and bitwise operations wins, then the smallest:
    BnotOr_FactorRule_1 and PredOr2_Rule_1 both match 3 & ~(x * x) at the same size, but
    only PredOr2_Rule_1 removes the multiplication.
    """
    best, best_key = None, None
    for rule in rules:
        if rule.name in dead:
            continue
        try:
            rewrite = rule.match(node)
        except Exception as exc:
            dead.add(rule.name)
            errors.append((rule.name, f"{type(exc).__name__}: {exc}"))
            continue
        if rewrite is None or rewrite.replacement in chain or node_count(rewrite.replacement) > node_count(node):
            continue
        key = (is_mba(rewrite.replacement), node_count(rewrite.replacement))
        if best_key is None or key < best_key:
            best, best_key = rewrite, key
    return best


def _tier2(e, backends, limits, dead, sources, out: Outcome, check: Checker,
           shared: Deadline = Deadline.never()) -> tuple[Expr, bool]:
    """Simplify MBA subtrees top-down. Returns (result, whether a time budget ran out)."""
    deadline = Deadline.after(limits.tier2_seconds).earlier(shared)
    exhausted = False
    has_linear = any(getattr(b, "abstracts_atoms", False) for b in backends)

    def attempt(node: Expr, abstracting_only: bool) -> Optional[Expr]:
        """The first backend result that is better and not refuted. Backends that bound their own
        atom count (the linear one: (A ^ B) + 2 * (~A | B) over sums A and B) run on any node; the
        others only within the variable limit."""
        nonlocal exhausted
        within_limit = len(variables(node)) <= limits.max_vars
        # The linear backend decides linear MBAs by their signature; on 102 such real-sample
        # queries CoBRA never found anything smaller (1.6 s spent).
        linear_decided = has_linear and is_linear_mba(node)
        for backend in backends:
            abstracting = getattr(backend, "abstracts_atoms", False)
            if (abstracting_only and not abstracting) or not (within_limit or abstracting):
                continue
            if linear_decided and not abstracting:
                continue
            source = f"tier2:{backend.name}"
            if source in dead:
                continue
            if deadline.expired():
                exhausted = True
                return None
            try:
                result = backend.simplify(node, deadline=deadline)
            except Exception as exc:
                dead.add(source)
                out.errors.append((source, f"{type(exc).__name__}: {exc}"))
                continue
            if result is None or not _better(result, node):
                continue
            if _checked(check, node, result).verdict is Verdict.REFUTED:
                out.refuted += 1
                out.errors.append((source, f"refuted {to_str(node)} -> {to_str(result)}"))
                continue
            sources.append(source)
            return result
        return None

    def visit(node: Expr) -> Expr:
        if not is_mba(node):
            return node
        result = attempt(node, abstracting_only=False)
        if result is not None:
            # A backend may stop short (CoBRA leaves x + y - 2 * (x & y) beside x * y); each
            # accepted step is strictly better, so revisiting terminates.
            return visit(result)
        rebuilt = _rebuild(node, visit)
        if rebuilt != node and is_mba(rebuilt) and not exhausted:
            # Simplified children can leave few enough atoms for the linear backend.
            result = attempt(rebuilt, abstracting_only=True)
            if result is not None:
                return visit(result)
        return rebuilt

    return visit(e), exhausted
