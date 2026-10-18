"""How a constant address is used (v4.0, spec 3.2), over a use graph the view provides. No binaryninja import.

A table is never written when every use of its address only computes a load address: up through address
arithmetic to a load's source, and through variables (SSA definitions and merges) up to MAX_DEPTH copies. A
store's destination is a write. Anything else (a call argument, a stored value, a comparison, a return, a
partial read) lets the address escape, so the table may be written where the analysis cannot see.
"""

from __future__ import annotations

from typing import Hashable, Iterable, Optional, Protocol, Sequence

READ, WRITE, ESCAPE = "read", "write", "escape"
ADDRESS_OPS = frozenset({"add", "sub", "mul", "shl", "zx", "sx", "trunc"})
MAX_DEPTH = 8  # d810-ng's candidate_max_depth
_CARRIERS = frozenset({"const", "var"}) | ADDRESS_OPS


class UseGraph(Protocol):
    def kind(self, node: Hashable) -> str: ...

    def parent(self, node: Hashable) -> Optional[tuple[Hashable, str]]:
        """(the parent node, the operand's name in it), or None for a whole statement."""

    def readers(self, node: Hashable) -> Sequence[Hashable]:
        """For a "set_var" or "phi" node: the nodes that read the variable it defines."""


def classify(starts: Iterable[Hashable], graph: UseGraph, max_depth: int = MAX_DEPTH) -> tuple[str, Optional[Hashable]]:
    """(READ, None) when every start only reaches load addresses; else (WRITE or ESCAPE, the node where)."""
    work = [(node, 0) for node in starts]
    seen: set = set()
    while work:
        node, depth = work.pop()
        if node in seen:
            continue
        seen.add(node)
        kind = graph.kind(node)
        if kind == "phi":
            if depth >= max_depth:
                return ESCAPE, node
            work.extend((reader, depth + 1) for reader in graph.readers(node))
            continue
        if kind not in _CARRIERS:
            return ESCAPE, node  # a partial or aliased read cannot be followed
        up = graph.parent(node)
        if up is None:
            return ESCAPE, node
        parent, operand = up
        parent_kind = graph.kind(parent)
        if parent_kind in ADDRESS_OPS:
            work.append((parent, depth))
        elif parent_kind == "load" and operand == "src":
            continue
        elif parent_kind == "store" and operand == "dest":
            return WRITE, parent
        elif parent_kind == "set_var" and operand == "src":
            if depth >= max_depth:
                return ESCAPE, parent
            work.extend((reader, depth + 1) for reader in graph.readers(parent))
        else:
            return ESCAPE, parent
    return READ, None


def reaches_target(starts: Iterable[Hashable], graph: UseGraph, max_depth: int = MAX_DEPTH) -> bool:
    """Whether a value (read at `starts`) flows, through whole copies and merges only, into a call's or a
    jump's target: a wrapper's result used as a code pointer (spec 3.5)."""
    work = [(node, 0) for node in starts]
    seen: set = set()
    while work:
        node, depth = work.pop()
        if node in seen:
            continue
        seen.add(node)
        kind = graph.kind(node)
        if kind == "phi":
            if depth < max_depth:
                work.extend((reader, depth + 1) for reader in graph.readers(node))
            continue
        if kind != "var":
            continue
        up = graph.parent(node)
        if up is None:
            continue
        parent, operand = up
        parent_kind = graph.kind(parent)
        if parent_kind in ("call", "jump") and operand == "dest":
            return True
        if parent_kind == "set_var" and operand == "src" and depth < max_depth:
            work.extend((reader, depth + 1) for reader in graph.readers(parent))
    return False
