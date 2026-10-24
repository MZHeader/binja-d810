"""Exact backwards flag-use liveness over a bounded CFG; no architecture assumptions."""


def live_after(edges, reads, writes, tick=lambda: None):
    before = {node: set() for node in edges}
    after = {node: set() for node in edges}
    changed = True
    while changed:
        changed = False
        for node in reversed(tuple(edges)):
            tick()
            outgoing = set()
            for target in edges[node]:
                tick(); outgoing.update(before.get(target, ()))
            incoming = set(reads.get(node, ())) | (outgoing - set(writes.get(node, ())))
            after[node] = outgoing
            if incoming != before[node]: before[node] = incoming; changed = True
    return after
