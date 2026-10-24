"""Conservative joins and byte provenance for acyclic caller-frame proofs.

Unequal scalar values become independent unknowns. Stack-derived values never lose their
provenance: an ambiguous join rejects the proof. No branch feasibility is assumed.
"""

from collections import deque
from dataclasses import dataclass, field

from ..ir.expr import Var, nodes
from .frame import Frame


def order(successors, entry, tick):
    reachable, pending = set(), [entry]
    while pending:
        tick(); node = pending.pop()
        if node in reachable:
            continue
        if node not in successors:
            raise ValueError("incomplete caller control flow")
        reachable.add(node)
        pending.extend(successors[node])
    predecessors = {node: set() for node in reachable}
    for node in reachable:
        for target in successors[node]:
            tick(); predecessors[target].add(node)
    degree = {node: len(preds) for node, preds in predecessors.items()}
    pending = deque(sorted(node for node, count in degree.items() if not count))
    ordered = []
    while pending:
        tick(); node = pending.popleft(); ordered.append(node)
        for target in sorted(set(successors[node])):
            degree[target] -= 1
            if not degree[target]:
                pending.append(target)
    if len(ordered) != len(reachable):
        raise ValueError("caller-frame loop is not modeled")
    return ordered, predecessors


@dataclass
class Effects:
    live: set = field(default_factory=set)
    outside: set = field(default_factory=set)
    stores: dict = field(default_factory=dict)


class TrackedFrame(Frame):
    def __init__(self, *args, effects=None):
        super().__init__(*args)
        self.effects = effects if effects is not None else Effects()
        self.owner = None
        self.origins = {}
        self.stack_position = lambda: None

    def copy(self):
        result = super().copy()
        result.effects, result.owner = self.effects, self.owner
        result.origins = self.origins.copy()  # immutable sets of possible byte writers
        result.stack_position = self.stack_position
        return result

    def read(self, offset, size):
        for at in range(offset, offset + size):
            self.effects.live.update(self.origins.get(at, frozenset()) - {self.owner})
        return super().read(offset, size)

    def write(self, offset, value):
        super().write(offset, value)
        if self.owner is not None:
            stack = self.stack_position()
            if stack is None or offset < stack or offset + value.width // 8 > 0:
                self.effects.outside.add(self.owner)
        for at in range(offset, offset + value.width // 8):
            self.origins[at] = frozenset() if self.owner is None else frozenset({self.owner})
            if self.owner is not None:
                self.effects.stores.setdefault(self.owner, set()).add(at)


def join(inputs, key, stack_name, tick):
    """One fresh state per CFG block; the input states are never mutated."""
    if not inputs:
        raise ValueError("caller block has no incoming state")
    states, frames = zip(*inputs)
    tick(len(states[0].values) + len(frames[0].bytes) + len(frames[0].whole))
    state, frame = states[0].copy(), frames[0].copy()
    if len(inputs) == 1:
        return state, frame

    def merged(values, name):
        tick(len(values))
        first = values[0]
        if all(value == first for value in values[1:]):
            return first
        for value in values:
            for node in nodes(value):
                tick()
                if isinstance(node, Var) and node.name == stack_name:
                    raise ValueError("ambiguous frame-derived value at caller merge")
        if any(value.width != first.width for value in values):
            raise ValueError("caller merge width mismatch")
        return Var(f"caller-merge:{key}:{name}", first.width)

    state.values = {name: merged([s.values[name] for s in states], f"register:{name}")
                    for name in state.values}
    state.written = set().union(*(s.written for s in states))
    state.live_in = set().union(*(s.live_in for s in states))
    addresses = set().union(*(f.bytes for f in frames))
    if len(addresses) > frame.max_bytes:
        raise ValueError("caller merge frame byte limit")
    frame.bytes = {at: merged([f.bytes.get(at, Var(f"stack:{at:+d}", 8)) for f in frames], f"byte:{at}")
                   for at in sorted(addresses)}
    frame.inputs = set().union(*(f.inputs for f in frames))
    frame.written = set().union(*(f.written for f in frames))
    frame.whole = {span: value for span, value in frame.whole.items()
                   if all(f.whole.get(span) == value for f in frames)}
    frame.origins = {}
    for at in set().union(*(f.origins for f in frames)):
        tick(len(frames))
        frame.origins[at] = frozenset().union(*(f.origins.get(at, frozenset()) for f in frames))
    return state, frame
