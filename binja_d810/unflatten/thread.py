"""Jump threading through dispatchers (v4.2, spec §3.1-3.4): simulate the path from an edge, then plan
which edges get their own copy of the path to the block they reach."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from typing import Mapping, Optional

from ..deadline import Deadline
from ..ir.expr import CMP_OPS, BinOp, Const, Expr, Var, substitute, variables
from .graph import Branch, Goto, Graph, Jump, Key, Phi, dominators, key_of, reaches
from .values import Values, outcomes

MAX_STEPS = 64
MAX_COPIED = 64
MAX_DUP = 8  # side effects copied before the first dispatcher block
MIN_STATES = 3  # distinct constants a state variable is compared with, without other evidence


@dataclass(frozen=True)
class Outcome:
    path: tuple[int, ...]  # the blocks run, in order (a block may repeat)
    dest: int  # where the path stopped: the block the copy of the path jumps to
    decided: tuple[int, ...]  # the blocks whose terminator the path decided, in order


@dataclass(frozen=True)
class Split:
    """The edge splits on the value of `key` (spec §3.1). With a condition, live at the end of the
    predecessor, it picks `if_one` or `if_zero`; without one, a chain tests each value but the last."""
    key: Key
    condition: Optional[Expr]
    if_one: Optional[int]
    if_zero: Optional[int]
    outcomes: Mapping[int, Outcome]


@dataclass(frozen=True)
class Thread:
    """The edge pred -(slot)-> start rewritten to a copy of a path: `outcome`'s, or one per value of `split`."""
    pred: int
    slot: str  # "goto", "true" or "false"
    start: int
    outcome: Optional[Outcome]
    split: Optional[Split]


@dataclass(frozen=True)
class Plan:
    threads: tuple[Thread, ...]
    dispatcher: frozenset[int]
    states: frozenset[int]  # the Key.var of each evidenced state variable


_EMPTY = Plan((), frozenset(), frozenset())


def plan(graph: Graph, *, evidenced: frozenset[int] = frozenset(), deadline: Deadline = Deadline.never()) -> Plan:
    """The threads worth rewriting (spec §3.1-3.4). `evidenced` holds state variables an earlier round showed
    evidence for. Empty when the deadline passes."""
    values = Values(graph, deadline)
    doms = dominators(graph)
    threads = candidates(graph, values, doms)
    if deadline.expired():
        return _EMPTY
    found = select(graph, threads, evidenced)
    return _EMPTY if deadline.expired() else found


def candidates(graph: Graph, values: Values, doms: Mapping[int, frozenset[int]]) -> list[Thread]:
    """Every edge out of a goto or if into a block with at least two predecessors whose path decides
    something, directly or split on the value the edge carries."""
    out: list[Thread] = []
    known: dict[int, dict[Key, int]] = {}
    for start in graph.blocks():
        preds = graph.preds(start)
        if len(preds) < 2:
            continue
        for edge in preds:
            if edge.slot == "jump":
                continue
            if values.deadline.expired():
                return out
            if edge.source not in known:
                known[edge.source] = facts(graph, values, doms, edge.source)
            env = known[edge.source]
            outcome = simulate(graph, values, edge.source, start, env)
            if outcome.decided:
                out.append(Thread(edge.source, edge.slot, start, outcome, None))
                continue
            split = _split(graph, values, edge.source, start, env)
            if split is not None:
                out.append(Thread(edge.source, edge.slot, start, None, split))
    return out


def split_edge(graph: Graph, values: Values, doms: Mapping[int, frozenset[int]], pred: int,
               start: int) -> Optional[Split]:
    """A split of the undecided edge pred -> start on the first phi source whose value set decides something."""
    return _split(graph, values, pred, start, facts(graph, values, doms, pred))


def _split(graph: Graph, values: Values, pred: int, start: int, env: Mapping[Key, int]) -> Optional[Split]:
    for dest in graph.phis(start):
        key = graph.phi_source(start, pred, dest)
        if key is None or values.static(key) is not None:
            continue
        if isinstance(graph.definition(key), Phi) and graph.def_block(key) == pred:
            continue  # merged in the predecessor itself: the edges into it are threaded instead
        found = values.value_set(key)
        if found is None or len(found) < 2:
            continue
        condition, one, zero = _condition(graph, values, pred, key)
        if condition is not None and {one, zero} <= found:
            found = frozenset({one, zero})
        else:
            condition = one = zero = None
        paths = {value: simulate(graph, values, pred, start, {**env, key: value}) for value in sorted(found)}
        if any(o.decided for o in paths.values()):
            return Split(key, condition, one, zero, paths)
    return None


def _condition(graph: Graph, values: Values, pred: int, key: Key) -> tuple[Optional[Expr], Optional[int], Optional[int]]:
    """(c, value if c, value if not c) when `key` is a live expression of exactly one boolean c."""
    e = graph.live_expression(pred, key)
    table = None if e is None else outcomes(values.with_statics(e), values.deadline)
    if table is None or len(table[0]) != 1 or table[1][(0,)] == table[1][(1,)]:
        return None, None, None
    return table[0][0], table[1][(1,)], table[1][(0,)]


def select(graph: Graph, threads: list[Thread], evidenced: frozenset[int] = frozenset()) -> Plan:
    """Spec §3.4: the dispatcher blocks of evidenced state variables, and the threads through them, each cut
    after the dispatcher at the first handler with side effects."""
    counts = Counter(b for t in threads for b in set(_decided(t)))
    hot = sorted(b for b, n in counts.items() if n >= 2)
    reads: dict[int, set[int]] = {}
    compared: dict[int, set[int]] = defaultdict(set)
    jumped: set[int] = set()
    for b in hot:
        term = graph.terminator(b)
        if isinstance(term, Branch):
            reads[b] = _reads(term.cond)
            found = _compared(term.cond)
            if found is not None:
                compared[found[0]].add(found[1])
        elif isinstance(term, Jump):
            reads[b] = _reads(term.dest, *(load.address for load in term.loads.values()))
            jumped |= reads[b]
    states = {v for read in reads.values() for v in read
              if v in evidenced or v in jumped or len(compared[v]) >= MIN_STATES}
    dispatcher = frozenset(b for b, read in reads.items() if read & states)
    kept = [cut for cut in (_cut(graph, t, dispatcher) for t in threads) if cut is not None]
    edges = {(t.pred, t.start) for t in kept}
    final = tuple(t for t in kept if not _redundant(t, dispatcher, edges))
    return Plan(final, dispatcher, frozenset(states)) if final else _EMPTY


def _outcomes(thread: Thread) -> list[Outcome]:
    return [thread.outcome] if thread.split is None else list(thread.split.outcomes.values())


def _decided(thread: Thread) -> list[int]:
    return [b for o in _outcomes(thread) for b in o.decided]


def _reads(*exprs: Optional[Expr]) -> set[int]:
    return {key_of(v).var for e in exprs if e is not None for v in variables(e) if key_of(v) is not None}


def _compared(cond: Optional[Expr]) -> Optional[tuple[int, int]]:
    """(variable, constant) for a comparison of an expression of one variable with a constant."""
    if not isinstance(cond, BinOp) or cond.op not in CMP_OPS:
        return None
    for side, const in ((cond.left, cond.right), (cond.right, cond.left)):
        if isinstance(const, Const):
            read = _reads(side)
            if len(read) == 1:
                return read.pop(), const.value
    return None


def _cut(graph: Graph, thread: Thread, dispatcher: frozenset[int]) -> Optional[Thread]:
    if thread.split is None:
        cut = _truncate(graph, thread.outcome, dispatcher)
        if cut is None or cut.dest == thread.start:
            return None
        return replace(thread, outcome=cut)
    cuts = {}
    for value, o in thread.split.outcomes.items():
        cuts[value] = _truncate(graph, o, dispatcher)
        if cuts[value] is None:
            return None
    return replace(thread, split=replace(thread.split, outcomes=cuts))


def _truncate(graph: Graph, outcome: Outcome, dispatcher: frozenset[int]) -> Optional[Outcome]:
    """The path through the dispatcher, ending at the first block past it that is not a dispatcher block and
    has a side effect. None when the path misses the dispatcher, copies more than MAX_DUP side effects before
    it, or breaks a cap."""
    path, dest = outcome.path, outcome.dest
    first = next((i for i, b in enumerate(path) if b in dispatcher), None)
    if first is None or sum(_impure(graph, b) for b in path[:first]) > MAX_DUP:
        return None
    for j in range(first + 1, len(path)):
        if path[j] not in dispatcher and _impure(graph, path[j]):
            path, dest = path[:j], path[j]
            break
    if len(path) >= MAX_STEPS or sum(len(graph.steps(b)) for b in path) > MAX_COPIED:
        return None  # a path still MAX_STEPS long never left the dispatcher
    return Outcome(path, dest, tuple(b for b in path if not isinstance(graph.terminator(b), Goto)))


def _impure(graph: Graph, block: int) -> int:
    return sum(1 for step in graph.steps(block) if step.impure)


def _redundant(thread: Thread, dispatcher: frozenset[int], edges: set[tuple[int, int]]) -> bool:
    """Every path copies code before the dispatcher that a later threaded edge on its way there covers."""
    for o in _outcomes(thread):
        first = next(i for i, b in enumerate(o.path) if b in dispatcher)
        if not any((o.path[k - 1], o.path[k]) in edges for k in range(1, first + 1)):
            return False
    return True


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
