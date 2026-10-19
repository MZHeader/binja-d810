"""Toy graphs for the unflattening unit tests: a dict spec in place of MLIL (binja_d810.unflatten.graph).

A block spec has "phi" ({"s#2": {pred: "s#1" | None}}), "do" (statements: "t#1 = <expr>" is pure,
"t#1 = ?" is impure and writes t#1, "!" is impure and writes nothing) and one terminator: "goto": b,
"if": ("<cond>", t, f), "jump": ("<dest>", {"L": ("<address>", width)}, {value: b}) or "stop": True.
Names are variable#version, 32 bits unless `widths` says otherwise; a condition is two sides joined by
" == ", " != ", " u< " or " s< ". `memory` maps an address to its 32-bit value; `live` maps
(pred, "s#3") to the expression text live_expression returns.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional

from binja_d810.ir.expr import BinOp, Expr, Var, substitute, variables
from binja_d810.ir.parse import parse
from binja_d810.unflatten.graph import Assign, Branch, Edge, Goto, Jump, Key, Load, Phi, Step, Stop, Unknown

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*#\d+")
_CMP = {" == ": "eq", " != ": "ne", " u< ": "ult", " s< ": "slt"}


class ToyGraph:
    def __init__(self, spec: Mapping[int, dict], *, entry: int = 0, widths: Optional[Mapping[str, int]] = None,
                 memory: Optional[Mapping[int, int]] = None, live: Optional[Mapping[tuple, str]] = None):
        self.entry = entry
        self._widths = dict(widths or {})
        self._ids: dict[str, int] = {}
        self._memory = dict(memory or {})
        self._live = {(pred, self.key(text)): source for (pred, text), source in (live or {}).items()}
        self._phis: dict[int, dict[Key, dict[int, Optional[Key]]]] = {}
        self._steps: dict[int, tuple[Step, ...]] = {}
        self._terms: dict[int, object] = {}
        self._defs: dict[Key, object] = {}
        self._def_blocks: dict[Key, int] = {}
        for b, block in spec.items():
            phis = {}
            for dest, sources in block.get("phi", {}).items():
                d = self.key(dest)
                phis[d] = {p: None if s is None else self.key(s) for p, s in sources.items()}
                self._defs[d] = Phi(tuple(s for s in phis[d].values() if s is not None))
                self._def_blocks[d] = b
            self._phis[b] = phis
            self._steps[b] = tuple(self._step(b, text) for text in block.get("do", []))
            self._terms[b] = self._terminator(block)
        self._preds: dict[int, list[Edge]] = {b: [] for b in spec}
        for b in spec:
            for target, slot in _successors(self._terms[b]):
                self._preds[target].append(Edge(b, slot))

    # --- building ---

    def key(self, text: str) -> Key:
        name, version = text.split("#")
        return Key(self._ids.setdefault(name, len(self._ids) + 1), int(version))

    def bits(self, name: str) -> int:
        return self._widths.get(name, 32)

    def expr(self, text: str) -> Expr:
        for token, op in _CMP.items():
            if token in text:
                left, right = text.split(token)
                l, r = self.expr(left), self.expr(right)
                return BinOp(op, l, r, 1)
        names = set(_NAME.findall(text))
        e = parse(text, 32, {n: self.bits(n.split("#")[0]) for n in names})
        mapping = {v.name: Var(v.name, v.width, ref=self.key(v.name)) for v in variables(e) if "#" in v.name}
        return substitute(e, mapping)

    def _step(self, b: int, text: str) -> Step:
        if text == "!":
            return Step(None, None, (), True)
        dest, source = (part.strip() for part in text.split("=", 1))
        d = self.key(dest)
        self._def_blocks[d] = b
        if source == "?":
            self._defs[d] = Unknown()
            return Step(d, None, (d,), True)
        e = self.expr(source)
        self._defs[d] = Assign(e)
        return Step(d, e, (d,), False)

    def _terminator(self, block: dict):
        if "goto" in block:
            return Goto(block["goto"])
        if "if" in block:
            cond, t, f = block["if"]
            return Branch(self.expr(cond), t, f)
        if "jump" in block:
            dest, loads, targets = block["jump"]
            return Jump(self.expr(dest), {name: Load(self.expr(addr), width) for name, (addr, width) in loads.items()},
                        dict(targets))
        return Stop()

    # --- Graph protocol ---

    def blocks(self):
        return sorted(self._terms)

    def preds(self, b):
        return self._preds[b]

    def phis(self, b):
        return list(self._phis[b])

    def phi_source(self, b, pred, dest):
        return self._phis[b].get(dest, {}).get(pred)

    def steps(self, b):
        return self._steps[b]

    def terminator(self, b):
        return self._terms[b]

    def definition(self, key):
        return self._defs.get(key, Unknown())

    def def_block(self, key):
        return self._def_blocks.get(key)

    def width(self, key):
        names = {i: n for n, i in self._ids.items()}
        return self.bits(names[key.var])

    def read(self, address, width):
        value = self._memory.get(address)
        return None if value is None else value & ((1 << width) - 1)

    def live_expression(self, pred, key):
        source = self._live.get((pred, key))
        return None if source is None else self.expr(source)


def _successors(term) -> list[tuple[int, str]]:
    if isinstance(term, Goto):
        return [(term.target, "goto")]
    if isinstance(term, Branch):
        return [(term.true, "true"), (term.false, "false")]
    if isinstance(term, Jump):
        return [(b, "jump") for b in dict.fromkeys(term.targets.values())]
    return []


def toy(spec: Mapping[int, dict], **kwargs) -> ToyGraph:
    return ToyGraph(spec, **kwargs)
