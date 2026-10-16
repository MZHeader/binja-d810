"""Tier 2 fallback: simplifier for linear mixed boolean-arithmetic expressions.

A linear MBA is a sum of constant multiples of bitwise expressions over the same variables.
Every bit position evaluates independently, so such an expression is determined by its values
on inputs where each variable is 0 or 1 (its signature). We build candidate expressions with the
same signature, keep the smallest, and return it only if it is smaller than the input.

Non-linear subterms (x * y, x & 0xff, shifts, extensions) are first replaced by fresh variables
("atoms"). An identity that holds for every value of an atom also holds for the subterm it stands
for, so (x * y) + (x ^ y) + 2 * (x & y) still simplifies, to (x * y) + x + y.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Iterator, Optional

from ..ir.evaluate import evaluate
from ..ir.expr import BinOp, Const, Expr, UnOp, Var, mask, node_count, relink, substitute, variables, without_refs
from ..ir.normalize import normalize

_DP_MAX_SIZE = 15


def _is_bitwise(e: Expr) -> bool:
    if isinstance(e, Var):
        return True
    if isinstance(e, Const):
        return e.value in (0, mask(e.width))
    if isinstance(e, UnOp):
        return e.op == "not" and _is_bitwise(e.operand)
    return e.op in ("and", "or", "xor") and _is_bitwise(e.left) and _is_bitwise(e.right)


def is_linear_mba(e: Expr) -> bool:
    if isinstance(e, Const) or _is_bitwise(e):
        return True
    if isinstance(e, UnOp):
        return e.op == "neg" and is_linear_mba(e.operand)
    if e.op in ("add", "sub"):
        return is_linear_mba(e.left) and is_linear_mba(e.right)
    if e.op == "mul":
        if isinstance(e.left, Const):
            return is_linear_mba(e.right)
        if isinstance(e.right, Const):
            return is_linear_mba(e.left)
    return False


def _abstract(e: Expr, atoms: dict[Expr, Var], taken: set[str]) -> Expr:
    """Replace maximal non-linear subterms by variables; equal subterms share one variable. An
    operand of a bitwise operation that is not itself bitwise (a sum: (A ^ B) + 2 * (~A | B)) is an
    atom, so A ^ B and ~A | B keep their relation."""
    if isinstance(e, (Var, Const)):
        return e
    if isinstance(e, UnOp) and e.op == "neg":
        return UnOp("neg", _abstract(e.operand, atoms, taken), e.width)
    if isinstance(e, BinOp) and (e.op in ("add", "sub") or (
            e.op == "mul" and (isinstance(e.left, Const) or isinstance(e.right, Const)))):
        return BinOp(e.op, _abstract(e.left, atoms, taken), _abstract(e.right, atoms, taken), e.width)
    if isinstance(e, UnOp) and e.op == "not":
        return UnOp("not", _bitwise_operand(e.operand, atoms, taken), e.width)
    if isinstance(e, BinOp) and e.op in ("and", "or", "xor"):
        return BinOp(e.op, _bitwise_operand(e.left, atoms, taken), _bitwise_operand(e.right, atoms, taken), e.width)
    return _atom(e, atoms, taken)


def _bitwise_operand(e: Expr, atoms: dict[Expr, Var], taken: set[str]) -> Expr:
    abstracted = _abstract(e, atoms, taken)
    return abstracted if _is_bitwise(abstracted) else _atom(e, atoms, taken)


def _atom(e: Expr, atoms: dict[Expr, Var], taken: set[str]) -> Var:
    if e not in atoms:
        name = f"atom@{len(atoms)}"
        while name in taken:
            name += "_"
        taken.add(name)
        atoms[e] = Var(name, e.width)
    return atoms[e]


def abstract_atoms(e: Expr, atoms: dict[Expr, Var], taken: set[str]) -> Expr:
    """`e` with maximal non-linear subterms replaced by variables shared through `atoms`."""
    return _abstract(e, atoms, taken)


def signature(e: Expr, vs: list[Var]) -> tuple[int, ...]:
    return tuple(
        evaluate(e, {v.name: (i >> k) & 1 for k, v in enumerate(vs)}) for i in range(1 << len(vs))
    )


def _var_table(k: int, t: int) -> int:
    return sum(1 << i for i in range(1 << t) if (i >> k) & 1)


@lru_cache(maxsize=None)
def _minimal_formulas(t: int) -> dict[int, tuple]:
    """Smallest and/or/xor/not formula (nested tuples over variable indices) per truth table."""
    full = (1 << (1 << t)) - 1
    best: dict[int, tuple] = {}
    by_size: dict[int, list[int]] = {}

    def add(table: int, formula: tuple, size: int) -> None:
        if table not in best:
            best[table] = formula
            by_size.setdefault(size, []).append(table)

    for k in range(t):
        add(_var_table(k, t), ("var", k), 1)
    for size in range(2, _DP_MAX_SIZE + 1):
        for table in list(by_size.get(size - 1, [])):
            add(~table & full, ("not", best[table]), size)
        for left_size in range(1, size - 1):
            right_size = size - 1 - left_size
            for x in list(by_size.get(left_size, [])):
                for y in list(by_size.get(right_size, [])):
                    add(x & y, ("and", best[x], best[y]), size)
                    add(x | y, ("or", best[x], best[y]), size)
                    add(x ^ y, ("xor", best[x], best[y]), size)
        if len(best) == full + 1:
            break
    return best


def _anf_formula(table: int, t: int) -> tuple:
    n = 1 << t
    coeffs = [(table >> i) & 1 for i in range(n)]
    for k in range(t):  # Moebius transform over GF(2)
        for i in range(n):
            if (i >> k) & 1:
                coeffs[i] ^= coeffs[i ^ (1 << k)]
    terms = []
    for s in range(1, n):
        if coeffs[s]:
            vs = [("var", k) for k in range(t) if (s >> k) & 1]
            term = vs[0]
            for v in vs[1:]:
                term = ("and", term, v)
            terms.append(term)
    formula = terms[0]
    for term in terms[1:]:
        formula = ("xor", formula, term)
    return ("not", formula) if coeffs[0] else formula


def _formula_for(table: int, t: int) -> tuple:
    """Formula for a truth table that is neither all-zero nor all-one."""
    if t <= 3:
        found = _minimal_formulas(t).get(table)
        if found is not None:
            return found
    return _anf_formula(table, t)


def _to_expr(formula: tuple, vs: list[Var], width: int) -> Expr:
    kind = formula[0]
    if kind == "var":
        return vs[formula[1]]
    if kind == "not":
        return UnOp("not", _to_expr(formula[1], vs, width), width)
    return BinOp(kind, _to_expr(formula[1], vs, width), _to_expr(formula[2], vs, width), width)


def _scaled(coeff: int, term: Expr, width: int) -> Expr:
    if coeff == 1:
        return term
    if coeff == mask(width):
        return UnOp("neg", term, width)
    return BinOp("mul", Const(coeff, width), term, width)


def _plus(acc: Optional[Expr], term: Expr, width: int) -> Expr:
    return term if acc is None else BinOp("add", acc, term, width)


class LinearMBABackend:
    name = "linear"
    abstracts_atoms = True  # bounds its own atom count (max_vars), whatever the leaf variables

    def __init__(self, max_vars: int = 6, cache_size: int = 4096):
        self.max_vars = max_vars
        # Tier 2 asks again about the same subexpressions (each candidate of a chain, each pass).
        self._cache: "OrderedDict[Expr, Optional[Expr]]" = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def simplify(self, e: Expr, deadline=None) -> Optional[Expr]:
        with self._lock:
            if e in self._cache:
                self._cache.move_to_end(e)
                cached = self._cache[e]
                return None if cached is None else relink(cached, e)
        result = self._simplify(e)
        with self._lock:
            # No Binary Ninja references in a cache that outlives the binary (relinked on reuse).
            self._cache[without_refs(e)] = None if result is None else without_refs(result)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result

    def _simplify(self, e: Expr) -> Optional[Expr]:
        # Bounded by max_vars (at most 2**6 signature entries): no deadline checks needed.
        atoms: dict[Expr, Var] = {}
        linear = _abstract(e, atoms, {v.name for v in variables(e)})
        if not is_linear_mba(linear):
            return None
        vs = sorted(variables(linear), key=lambda v: v.name)
        if len(vs) > self.max_vars or any(v.width != e.width for v in vs):
            return None
        sig = signature(linear, vs)
        valid = []
        for candidate in self._candidates(sig, vs, e.width):
            candidate = normalize(candidate)
            if signature(candidate, vs) == sig:
                valid.append(candidate)
        if not valid:
            return None
        best = min(valid, key=node_count)
        if atoms:
            best = normalize(substitute(best, {atom.name: term for term, atom in atoms.items()}))
        return best if node_count(best) < node_count(e) else None

    def _candidates(self, sig: tuple[int, ...], vs: list[Var], w: int) -> Iterator[Expr]:
        m, t, n = mask(w), len(vs), len(sig)
        s0 = sig[0]
        if all(v == s0 for v in sig):
            yield Const(s0, w)
            return

        values = sorted(set(sig))
        if len(values) == 2:  # k + c*F, with F(0)=1 shifting the constant by 2c
            for base, other in ((values[0], values[1]), (values[1], values[0])):
                table = sum(1 << i for i in range(n) if sig[i] == other)
                c = (other - base) & m
                k = (base + 2 * c * (table & 1)) & m
                term = _scaled(c, _to_expr(_formula_for(table, t), vs, w), w)
                yield term if k == 0 else BinOp("add", Const(k, w), term, w)

        g = [(v - s0) & m for v in sig]
        coeff = list(g)
        for k in range(t):  # Moebius transform over the integers: conjunction basis
            for i in range(n):
                if (i >> k) & 1:
                    coeff[i] = (coeff[i] - coeff[i ^ (1 << k)]) & m
        conj: Optional[Expr] = None
        for s in range(1, n):
            if coeff[s]:
                members = [vs[k] for k in range(t) if (s >> k) & 1]
                term = members[0]
                for v in members[1:]:
                    term = BinOp("and", term, v, w)
                conj = _plus(conj, _scaled(coeff[s], term, w), w)
        if s0:
            conj = _plus(conj, Const(s0, w), w)
        yield conj if conj is not None else Const(s0, w)

        # Some single-variable terms plus one scaled bitwise remainder: z + (x ^ y) keeps z and
        # leaves x ^ y, while keeping every single term would leave -2 * (x & y) instead.
        singles = [coeff[1 << k] for k in range(t)]
        active = [k for k in range(t) if singles[k]]
        for chosen in range(1 << len(active)):
            kept = [k for j, k in enumerate(active) if (chosen >> j) & 1]
            resid = [(g[i] - sum(singles[k] for k in kept if (i >> k) & 1)) & m for i in range(n)]
            nonzero = set(resid) - {0}
            if len(nonzero) != 1:
                continue
            c = nonzero.pop()
            table = sum(1 << i for i in range(n) if resid[i] == c)
            expr = _scaled(c, _to_expr(_formula_for(table, t), vs, w), w)
            for k in kept:
                expr = _plus(expr, _scaled(singles[k], vs[k], w), w)
            if s0:
                expr = _plus(expr, Const(s0, w), w)
            yield expr
