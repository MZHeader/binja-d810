"""Control-flow unflattening over MLIL (v4.2): the graph model built from MLIL and SSA, and its rewrite."""

from __future__ import annotations

from typing import Sequence

from binaryninja import MediumLevelILOperation as Op

from ...deadline import Deadline
from ...unflatten.thread import MIN_STATES, _terminator_keys, plan
from ...unflatten.values import Values
from . import junk, rewrite, selfcall, splice
from .graph import MLILGraph

MAX_ROUNDS = 4
GROWTH = 4  # a round is not applied when the function would grow past max(GROWTH x original, original + slack)
GROWTH_SLACK = 2048


def has_table_dispatcher(mlil, view, *, deadline: Deadline, ranges=()) -> bool:
    """A bounded preflight for reserving time after MBA: a jump table on a cycle reads constant-only state.
    This does not change the IL or accept a rewrite; the ordinary planner must still prove its threads.
    """
    blocks = []
    for block in mlil.basic_blocks:
        if deadline.expired():
            return False
        last = mlil[block.end - 1]
        if last.operation == Op.MLIL_JUMP_TO and len(last.targets) >= MIN_STATES:
            blocks.append(block.index)
    if not blocks:
        return False
    graph = MLILGraph(mlil, view=view, ranges=ranges)
    values = Values(graph, deadline)
    for block in blocks:
        if deadline.expired():
            return False
        if not any(values.only_constants(k) for k in _terminator_keys(graph.terminator(block))):
            continue
        work, seen = [e.source for e in graph.preds(block)], set()
        while work and not deadline.expired():
            pred = work.pop()
            if pred == block:
                return True
            if pred not in seen:
                seen.add(pred)
                work.extend(e.source for e in graph.preds(pred))
    return False


def _looped(mlil, view, ranges, deadline: Deadline):
    """(graph, plan, report lines) over the MLIL with its self tail calls turned into back edges (selfcall), when
    the plan threads a path through the loop head (one of them); else the same with its handlers spliced in
    first (splice); else None, and the calls stay calls."""
    found = _loop(mlil, view, ranges, deadline)
    if found is not None or deadline.expired():
        return found
    spliced = splice.handlers(mlil, view)
    if spliced is None:
        return None
    found = _loop(spliced[0], view, ranges, deadline)
    return None if found is None else (found[0], found[1], [f"{spliced[1]} handlers spliced in", *found[2]])


def _loop(mlil, view, ranges, deadline: Deadline):
    found = selfcall.loop(mlil, view)
    if found is None:
        return None
    looped, head, count = found
    graph = MLILGraph(looped, view=view, ranges=ranges, passed=frozenset(mlil.source_function.parameter_vars))
    planned = plan(graph, deadline=deadline)
    paths = [o.path for t in planned.threads for o in ((t.outcome,) if t.split is None else t.split.outcomes.values())]
    if not any(head in path for path in paths):
        return None
    return graph, planned, [f"{count} self calls in tail position read as a loop"]


def run(ctx, *, deadline: Deadline, ranges: Sequence[tuple[int, int]]) -> list[str]:
    """Rounds of planning and rewriting ctx.mlil, each committed to ctx.mlil. One report line per round."""
    lines: list[str] = []
    mlil = ctx.mlil
    original = len(mlil)
    cap = max(GROWTH * original, original + GROWTH_SLACK)
    evidenced: frozenset[int] = frozenset()
    removed = junk.loops(mlil, ctx.function.view)
    if removed is not None:
        ctx.mlil = mlil = removed[0]
        lines.append(f"{removed[1]} junk loops removed")
    first = _looped(mlil, ctx.function.view, ranges, deadline)
    if first is not None:
        mlil = first[0].mlil
    for n in range(1, MAX_ROUNDS + 1):
        if n == 1 and first is not None:
            graph, found, report = first
            lines.extend(report)
        else:
            graph = MLILGraph(mlil, view=ctx.function.view, ranges=ranges)
            found = plan(graph, evidenced=evidenced, deadline=deadline)
        if not found.threads:
            break
        evidenced |= found.states
        new = rewrite.rewrite(graph, found)
        if len(new) > cap:
            lines.append(f"round {n}: not applied, {len(new)} instructions over the cap of {cap}")
            break
        ctx.mlil = mlil = new
        names = ", ".join(sorted(graph.name(v) for v in found.states))
        lines.append(f"round {n}: {len(found.threads)} edges threaded through {len(found.dispatcher)} dispatcher "
                     f"blocks, state {names}")
    return lines
