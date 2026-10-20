"""Values of SSA keys independent of the path (spec §3.2): static values, value sets and evaluation."""

from __future__ import annotations

import itertools
from typing import Mapping, Optional

from ..deadline import Deadline
from ..ir.evaluate import evaluate
from ..ir.expr import Const, Expr, Var, substitute, variables
from ..semantic.constancy import constant_value
from ..semantic.selects import booleans, replace
from ..semantic.verify import Verdict
from .graph import Assign, Graph, Key, Phi, Undefined, key_of

MAX_VALUES = 16
MAX_WEB = 256
MAX_BOOLEANS = 3


class Values:
    def __init__(self, graph: Graph, deadline: Deadline = Deadline.never()):
        self.graph = graph
        self.deadline = deadline
        self._static: dict[Key, Optional[int]] = {}
        self._active: set[Key] = set()
        self._sets: dict[Key, Optional[frozenset[int]]] = {}
        self._held: dict[Key, bool] = {}

    def static(self, key: Key) -> Optional[int]:
        """The value `key` has on every path: a constant, an expression of static values, or a phi whose
        sources all have the same static value. None inside a cycle."""
        if key in self._static:
            return self._static[key]
        if key in self._active:
            return None
        self._active.add(key)
        try:
            d = self.graph.definition(key)
            value = None
            if isinstance(d, Phi) and d.sources:
                found = {self.static(s) for s in d.sources}
                if len(found) == 1:
                    value = found.pop()
            elif isinstance(d, Assign):
                value = self.eval(d.expr, {})
        finally:
            self._active.discard(key)
        self._static[key] = value
        return value

    def eval(self, e: Expr, env: Mapping[Key, int]) -> Optional[int]:
        """`e` with every leaf from `env`, else its static value; None when a leaf is unknown."""
        bindings = {}
        for leaf in variables(e):
            key = key_of(leaf)
            if key is None:
                return None
            value = env.get(key)
            if value is None:
                value = self.static(key)
            if value is None:
                return None
            bindings[leaf.name] = value
        try:
            return evaluate(e, bindings)
        except ArithmeticError:
            return None

    def value_set(self, key: Key) -> Optional[frozenset[int]]:
        """Every value `key` can hold: the leaves of its phi/copy web are static constants, or expressions
        over at most MAX_BOOLEANS conditions that are a proven constant for each assignment of them."""
        if key in self._sets:
            return self._sets[key]
        found = self._value_set(key, MAX_VALUES)
        self._sets[key] = found
        return found

    def only_constants(self, key: Key) -> bool:
        """`key` only ever holds constants: value_set without its MAX_VALUES cap (a machine has more states),
        and with Undefined leaves skipped: a slot read before its first store adds no value."""
        if key not in self._held:
            found = self._value_set(key, None, undefined=True)
            self._held[key] = bool(found)
        return self._held[key]

    def _value_set(self, key: Key, cap: Optional[int], *, undefined: bool = False) -> Optional[frozenset[int]]:
        work, seen, out = [key], set(), set()
        while work:
            k = work.pop()
            if k in seen:
                continue
            seen.add(k)
            if len(seen) > MAX_WEB or self.deadline.expired():
                return None
            value = self.static(k)
            if value is not None:
                out.add(value)
            else:
                d = self.graph.definition(k)
                if isinstance(d, Phi):
                    work.extend(d.sources)
                    continue
                if undefined and isinstance(d, Undefined):
                    continue
                if not isinstance(d, Assign):
                    return None
                e = d.expr
                if isinstance(e, Var) and key_of(e) is not None and e.width == self.graph.width(key_of(e)):
                    work.append(key_of(e))  # a copy
                    continue
                table = outcomes(self.with_statics(e), self.deadline)
                if table is None:
                    return None
                out |= set(table[1].values())
            if cap is not None and len(out) > cap:
                return None
        return frozenset(out)

    def with_statics(self, e: Expr) -> Expr:
        """`e` with each leaf that has a static value replaced by that constant."""
        mapping = {}
        for leaf in variables(e):
            key = key_of(leaf)
            value = None if key is None else self.static(key)
            if value is not None:
                mapping[leaf.name] = Const(value, leaf.width)
        return substitute(e, mapping) if mapping else e


def outcomes(e: Expr, deadline: Deadline) -> Optional[tuple[list[Expr], dict[tuple[int, ...], int]]]:
    """`e`'s value for each 0/1 assignment of its conditions (at most MAX_BOOLEANS), each a proven constant."""
    conds = booleans(e)
    if len(conds) > MAX_BOOLEANS:
        return None
    table: dict[tuple[int, ...], int] = {}
    for bits in itertools.product((0, 1), repeat=len(conds)):
        fixed = e
        for cond, bit in zip(conds, bits):
            fixed = replace(fixed, cond, Const(bit, 1))
        if not variables(fixed):
            try:
                table[bits] = evaluate(fixed, {})
            except ArithmeticError:
                return None
            continue
        found = constant_value(fixed, deadline)
        if found is None or found[1].verdict is not Verdict.PROVEN:
            return None
        table[bits] = found[0]
    return conds, table
