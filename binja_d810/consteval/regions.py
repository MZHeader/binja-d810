"""Structural proof for replacing a deterministic path through a general CFG."""


def reverse(edges):
    result = {node: set() for node in edges}
    for source, targets in edges.items():
        for target in targets:
            result.setdefault(target, set()).add(source)
    return result


def reachable(edges, start, tick=lambda: None):
    pending, seen = [start], set()
    while pending:
        tick()
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(edges.get(node, ()))
    return seen


def validate(edges, predecessors, start, exit, visited, tick=lambda: None):
    if start not in visited or exit in visited:
        raise ValueError("constant region has no separate entry and exit")
    interior_entries = set()
    for node in visited:
        tick()
        if node != start and any(p not in visited for p in predecessors.get(node, ())):
            interior_entries.add(node)
    if interior_entries and start != 0:
        bypass = dict(edges)
        bypass[start] = ()
        if reachable(bypass, 0, tick) & interior_entries:
            raise ValueError("constant region has an additional entry")
    after = reachable(edges, exit, tick)
    if after & visited:
        raise ValueError("retained control flow can reenter the evaluated region")
    return after
