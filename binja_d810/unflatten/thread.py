"""Jump threading through dispatchers (v4.2, spec §3.1-3.4): simulate the path from an edge, then plan
which edges get their own copy of the path to the block they reach."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from ..ir.expr import BinOp, Const, Var, substitute
from .graph import Branch, Goto, Graph, Jump, Key, key_of, reaches
from .values import Values

MAX_STEPS = 64


@dataclass(frozen=True)
class Outcome:
    path: tuple[int, ...]  # the blocks run, in order (a block may repeat)
    dest: int  # where the path stopped: the block the copy of the path jumps to
    decided: tuple[int, ...]  # the blocks whose terminator the path decided, in order


def facts(graph: Graph, values: Values, doms: Mapping[int, frozenset[int]], block: int) -> dict[Key, int]:
    """v == C at the end of `block`: a test of v == C (or the false edge of v != C) whose successor has that
    test as its only predecessor and dominates `block`, and that v's definition cannot reach `block` again
    without crossing the tested edge."""
    out: dict[Key, int] = {}
    for s in doms.get(block, ()):
        preds = graph.preds(s)
        if len(preds) != 1:
            continue
        test, slot = preds[0].source, preds[0].slot
        term = graph.terminator(test)
        if not isinstance(term, Branch) or term.cond is None or term.true == term.false:
            continue
        found = _equality(graph, term.cond, slot)
        if found is None:
            continue
        key, value = found
        start = graph.def_block(key)
        if reaches(graph, graph.entry if start is None else start, block, without=(test, s)):
            continue
        out[key] = value
    return out


def _equality(graph: Graph, cond, slot: str) -> Optional[tuple[Key, int]]:
    if not isinstance(cond, BinOp) or (cond.op, slot) not in (("eq", "true"), ("ne", "false")):
        return None
    for leaf, const in ((cond.left, cond.right), (cond.right, cond.left)):
        if isinstance(leaf, Var) and isinstance(const, Const):
            key = key_of(leaf)
            if key is not None and leaf.width == graph.width(key):
                return key, const.value
    return None


def simulate(graph: Graph, values: Values, pred: int, start: int, env: Mapping[Key, int]) -> Outcome:
    """Run from the edge pred -> start while every terminator is decided. A phi or assignment whose value
    is unknown removes it from the environment, so a block run again never keeps an older value."""
    env = dict(env)
    path: list[int] = []
    decided: list[int] = []
    came, cur = pred, start
    while len(path) < MAX_STEPS:
        entered = {}
        for dest in graph.phis(cur):  # together: a phi may read another phi of the same block
            src = graph.phi_source(cur, came, dest)
            entered[dest] = None if src is None else (env[src] if src in env else values.static(src))
        for dest, value in entered.items():
            _set(env, dest, value)
        for step in graph.steps(cur):
            if not step.impure and step.dest is not None and step.expr is not None:
                _set(env, step.dest, values.eval(step.expr, env))
            else:
                for written in step.writes:
                    env.pop(written, None)
        nxt = _next(graph, values, cur, env)
        if nxt is None:
            break
        target, was_decided = nxt
        if was_decided:
            decided.append(cur)
        path.append(cur)
        came, cur = cur, target
    return Outcome(tuple(path), cur, tuple(decided))


def _set(env: dict, key: Key, value: Optional[int]) -> None:
    if value is None:
        env.pop(key, None)
    else:
        env[key] = value


def _next(graph: Graph, values: Values, block: int, env: Mapping[Key, int]) -> Optional[tuple[int, bool]]:
    """(successor, decided) for the block's terminator under `env`; None where the path stops."""
    term = graph.terminator(block)
    if isinstance(term, Goto):
        return term.target, False
    if isinstance(term, Branch):
        value = None if term.cond is None else values.eval(term.cond, env)
        if value is None:
            return None
        return (term.true if value else term.false), True
    if isinstance(term, Jump):
        target = _jump_target(graph, values, term, env)
        return None if target is None else (target, True)
    return None


def _jump_target(graph: Graph, values: Values, term: Jump, env: Mapping[Key, int]) -> Optional[int]:
    if term.dest is None:
        return None
    loaded = {}
    for name, load in term.loads.items():
        address = values.eval(load.address, env)
        value = None if address is None else graph.read(address, load.width)
        if value is None:
            return None
        loaded[name] = Const(value, load.width)
    value = values.eval(substitute(term.dest, loaded), env)
    return None if value is None else term.targets.get(value)
