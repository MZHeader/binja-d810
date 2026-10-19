"""Control-flow unflattening over MLIL (v4.2): the graph model built from MLIL and SSA, and its rewrite."""

from __future__ import annotations

from typing import Sequence

from ...deadline import Deadline
from ...unflatten.thread import plan
from . import rewrite
from .graph import MLILGraph

MAX_ROUNDS = 4
GROWTH = 4  # a round is not applied when the function would grow past max(GROWTH x original, original + slack)
GROWTH_SLACK = 2048


def run(ctx, *, deadline: Deadline, ranges: Sequence[tuple[int, int]]) -> list[str]:
    """Rounds of planning and rewriting ctx.mlil, each committed to ctx.mlil. One report line per round."""
    lines: list[str] = []
    mlil = ctx.mlil
    original = len(mlil)
    cap = max(GROWTH * original, original + GROWTH_SLACK)
    evidenced: frozenset[int] = frozenset()
    for n in range(1, MAX_ROUNDS + 1):
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
