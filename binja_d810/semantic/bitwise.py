"""Tier 2 for bitwise-only roots: a normal form over classes of bit positions.

A bitwise expression computes each bit from the same bit of its inputs. With constants in it, a
bit's function also depends on the constants' bits at that position, so the positions fall into
classes (at most 2**c for c distinct constants), and every bit of a class has the same truth table
over the variables. Setting each variable to 0 or all-ones reads every class's table at once, in
2**t evaluations.

The result has one term per pair of complementary tables, (f ^ X) & A, or'ed with the classes that
are always one: (~x & 0x55) | (x & 0xaa) is (x ^ 0x55) & 0xff, which atoms cannot see (the linear
backend treats 0x55 and 0xaa as unrelated values). Other subterms (sums, shifts, extensions) are
atoms, as in the linear backend.
"""

from __future__ import annotations

from typing import Optional

from ..deadline import Deadline
from ..ir.evaluate import evaluate_shared
from ..ir.expr import BinOp, Const, Expr, UnOp, Var, mask, node_count, nodes, substitute, variables
from ..ir.normalize import normalize
from .linear_mba import _formula_for, _to_expr


def _abstract(e: Expr, atoms: dict[Expr, Var], taken: set[str], memo: dict[int, Expr]) -> Expr:
    """`e` with every maximal subterm that is not and, or, xor or not replaced by a variable."""
    found = memo.get(id(e))
    if found is not None:
        return found
    if isinstance(e, (Var, Const)):
        found = e
    elif isinstance(e, UnOp) and e.op == "not":
        found = UnOp("not", _abstract(e.operand, atoms, taken, memo), e.width)
    elif isinstance(e, BinOp) and e.op in ("and", "or", "xor"):
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


def _size(formula: tuple) -> int:
    return 1 if formula[0] == "var" else 1 + sum(_size(f) for f in formula[1:])


class BitwiseBackend:
    name = "bitwise"
    abstracts_atoms = True  # bounds its own name count, whatever the leaf variables
    requires_proof = True  # a timeout must never downgrade a new masked rewrite to sampling

    def __init__(self, *, mask_factoring=True, max_names=4, max_constants=4):
        self.mask_factoring = mask_factoring
        self.max_names = max_names
        self.max_constants = max_constants

    def simplify(self, e: Expr, deadline=None) -> Optional[Expr]:
        deadline = deadline or Deadline.never()
        if deadline.expired():
            return None
        if not isinstance(e, (BinOp, UnOp)) or e.op not in ("and", "or", "xor", "not"):
            return None
        atoms: dict[Expr, Var] = {}
        shape = _abstract(e, atoms, {v.name for v in variables(e)}, {})
        names = sorted(variables(shape), key=lambda v: v.name)
        constants = sorted({n.value for n in nodes(shape) if isinstance(n, Const)})
        if len(names) > self.max_names or len(constants) > self.max_constants:
            return None
        w, m, t = e.width, mask(e.width), len(names)
        results = []
        for i in range(1 << t):
            if deadline.expired():
                return None
            results.append(evaluate_shared(shape, {v.name: m if (i >> k) & 1 else 0 for k, v in enumerate(names)}))
        classes: dict[tuple, int] = {}  # the constants' bits at a position -> those positions
        for p in range(w):
            if deadline.expired():
                return None
            key = tuple((c >> p) & 1 for c in constants)
            classes[key] = classes.get(key, 0) | (1 << p)
        full = (1 << (1 << t)) - 1
        ones = 0
        pairs: dict[int, dict[int, int]] = {}  # a table and its complement -> {table: positions}
        for positions in classes.values():
            p = (positions & -positions).bit_length() - 1
            table = sum(((r >> p) & 1) << i for i, r in enumerate(results))
            if table == full:
                ones |= positions
            elif table:
                pair = pairs.setdefault(min(table, table ^ full), {})
                pair[table] = pair.get(table, 0) | positions
        terms = []
        for tables in pairs.values():
            if deadline.expired():
                return None
            # The smaller formula is written out; the other table's positions flip it.
            base = min(tables, key=lambda tb: (_size(_formula_for(tb, t)), -bin(tables[tb]).count("1")))
            within = sum(tables.values())
            term = _to_expr(_formula_for(base, t), names, w)
            flip = within & ~tables[base]
            if flip:
                term = BinOp("xor", term, Const(flip, w), w)
            # Outside its positions the term may be one only where the result is one anyway.
            if (within | ones) != m or len(pairs) > 1:
                term = BinOp("and", term, Const(within, w), w)
            terms.append(term)
        if ones:
            terms.append(Const(ones, w))
        result = terms[0] if terms else Const(0, w)
        for term in terms[1:]:
            result = BinOp("or", result, term, w)
        candidates = [result]
        if self.mask_factoring:
            factored = _masked_polynomial(results, names, w, deadline)
            if factored is not None:
                candidates.append(factored)
        if deadline.expired():
            return None
        bindings = {atom.name: term for term, atom in atoms.items()}
        candidates = [normalize(substitute(c, bindings) if atoms else c) for c in candidates]
        result = min(candidates, key=node_count)
        return result if node_count(result) < node_count(e) else None


def _masked_polynomial(results: list[int], names: list[Var], width: int, deadline: Deadline) -> Optional[Expr]:
    """Boolean algebraic normal form, with a word mask for each monomial's coefficient.

    The Mobius transform operates on all bit positions together. Unlike independent bit classes,
    it can share x between x outside a mask and x^y inside it. No particular mask or operator
    pattern is recognized. The caller's usual exact verifier still gates every emitted rewrite.
    """
    coefficients = list(results)
    for k in range(len(names)):
        if deadline.expired():
            return None
        for row in range(len(coefficients)):
            if row & (1 << k):
                coefficients[row] ^= coefficients[row ^ (1 << k)]
    terms = []
    for row, coefficient in enumerate(coefficients):
        if deadline.expired():
            return None
        if not coefficient:
            continue
        factors = [v for k, v in enumerate(names) if row & (1 << k)]
        term = factors[0] if factors else Const(mask(width), width)
        for factor in factors[1:]:
            term = BinOp("and", term, factor, width)
        if coefficient != mask(width):
            term = BinOp("and", term, Const(coefficient, width), width)
        terms.append(term)
    result = terms[0] if terms else Const(0, width)
    for term in terms[1:]:
        result = BinOp("xor", result, term, width)
    return result
