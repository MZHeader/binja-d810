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

from ..ir.evaluate import evaluate
from ..ir.expr import BinOp, Const, Expr, UnOp, Var, mask, node_count, nodes, substitute, variables
from ..ir.normalize import normalize
from .linear_mba import _formula_for, _to_expr

_MAX_NAMES = 4
_MAX_CONSTANTS = 4


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

    def simplify(self, e: Expr, deadline=None) -> Optional[Expr]:
        # At most 2**4 evaluations: no deadline checks needed.
        if not isinstance(e, (BinOp, UnOp)) or e.op not in ("and", "or", "xor", "not"):
            return None
        atoms: dict[Expr, Var] = {}
        shape = _abstract(e, atoms, {v.name for v in variables(e)}, {})
        names = sorted(variables(shape), key=lambda v: v.name)
        constants = sorted({n.value for n in nodes(shape) if isinstance(n, Const)})
        if len(names) > _MAX_NAMES or len(constants) > _MAX_CONSTANTS:
            return None
        w, m, t = e.width, mask(e.width), len(names)
        results = [evaluate(shape, {v.name: m if (i >> k) & 1 else 0 for k, v in enumerate(names)})
                   for i in range(1 << t)]
        classes: dict[tuple, int] = {}  # the constants' bits at a position -> those positions
        for p in range(w):
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
        if atoms:
            result = substitute(result, {atom.name: term for term, atom in atoms.items()})
        result = normalize(result)
        return result if node_count(result) < node_count(e) else None
