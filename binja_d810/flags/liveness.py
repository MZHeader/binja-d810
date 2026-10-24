"""Exact backwards flag-use liveness over a bounded CFG; no architecture assumptions."""

from collections import deque


def live_after(edges, reads, writes, tick=lambda: None):
    before = {node: set() for node in edges}
    after = {node: set() for node in edges}
    predecessors = {node:set() for node in edges}
    for node,targets in edges.items():
        tick()
        for target in targets:
            tick()
            if target in predecessors: predecessors[target].add(node)
    pending = deque(reversed(tuple(edges)))
    queued = set(edges)
    while pending:
        tick(); node = pending.popleft(); queued.remove(node)
        outgoing = set()
        for target in edges[node]:
            tick(); outgoing.update(before.get(target, ()))
        incoming = set(reads.get(node, ())) | (outgoing - set(writes.get(node, ())))
        after[node] = outgoing
        if incoming != before[node]:
            before[node] = incoming
            for pred in sorted(predecessors[node]):
                tick()
                if pred not in queued:
                    pending.append(pred); queued.add(pred)
    return after
