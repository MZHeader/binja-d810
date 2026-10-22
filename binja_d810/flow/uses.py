"""How a constant address is used (v4.0, spec 3.2), over a use graph the view provides. No binaryninja import.

A table is never written when every use of its address only computes a load address: up through address
arithmetic to a load's source, and through variables (SSA definitions and merges) up to MAX_DEPTH copies. A
store's destination is a write. Anything else (an unproved call argument, a stored value, a comparison, a return, a
partial read) lets the address escape, so the table may be written where the analysis cannot see. A load or
store the graph knows reaches a constant address outside the object ("load_outside", "store_outside": an arm64
page address a neighbour's access goes through) does not use it, nor does address arithmetic whose constant value lies past the object's end ("address_outside": the
page plus a neighbour's offset).

A key (v4.3, spec 3.2) is a value read from memory that encodes a constant: c - K, K ^ c, -K, K * c, put to
work (combined with a variable, returned, stored or passed), not only compared.
"""

from __future__ import annotations

from typing import Hashable, Iterable, Optional, Protocol, Sequence

READ, WRITE, ESCAPE = "read", "write", "escape"
ADDRESS_OPS = frozenset({"add", "sub", "mul", "shl", "zx", "sx", "trunc"})
MAX_DEPTH = 8  # d810-ng's candidate_max_depth
_CARRIERS = frozenset({"const", "var"}) | ADDRESS_OPS
ENCODING_OPS = frozenset({"add", "sub", "mul", "xor", "neg"})
_WIDTHS = frozenset({"zx", "sx", "trunc"})
# What a key's value may pass through on its way to a use, and the uses that put a constant to work.
_VALUE_OPS = ENCODING_OPS | _WIDTHS | frozenset({"and", "or", "not", "shl", "mlil_lsr", "mlil_asr", "mlil_ror", "mlil_rol"})
_MATERIAL = frozenset({("mlil_ret", "src"), ("store", "src"), ("call", "params")})


class UseGraph(Protocol):
    def kind(self, node: Hashable) -> str: ...

    def parent(self, node: Hashable) -> Optional[tuple[Hashable, str]]:
        """(the parent node, the operand's name in it), or None for a whole statement."""

    def readers(self, node: Hashable) -> Sequence[Hashable]:
        """For a "set_var" or "phi" node: the nodes that read the variable it defines."""

    def operands(self, node: Hashable) -> Sequence[tuple[str, Hashable]]:
        """(operand name, child node) for each of a node's operands (encodes only)."""


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
        elif parent_kind in ("load", "load_outside") and operand == "src":
            continue
        elif parent_kind == "store_outside" and operand == "dest":
            continue
        elif parent_kind == "address_outside":
            continue
        elif parent_kind == "store" and operand == "dest":
            return WRITE, parent
        elif parent_kind == "set_var" and operand == "src":
            if depth >= max_depth:
                return ESCAPE, parent
            work.extend((reader, depth + 1) for reader in graph.readers(parent))
        elif parent_kind == "call" and operand == "params" and \
                getattr(graph, "call_read_only", lambda *_: False)(parent, node):
            continue
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


def reaches_write(start: Hashable, graph: UseGraph, limit: int = 256) -> bool:
    """A nearby outside address feeds a store destination in this function. Only store chains
    supplement the ordinary reference search here; that search still checks the object's escapes.
    Exhausted searches include the candidate so the full proof can reject it conservatively.
    """
    work, seen = [(start, 0)], set()
    while work:
        node, depth = work.pop()
        if node in seen:
            continue
        seen.add(node)
        if len(seen) > limit or depth > MAX_DEPTH:
            return True
        kind = graph.kind(node)
        if kind == "phi":
            work.extend((reader, depth + 1) for reader in graph.readers(node))
            continue
        up = graph.parent(node)
        if kind not in _CARRIERS or up is None:
            continue
        parent, operand = up
        kind = graph.kind(parent)
        if kind == "store" and operand == "dest":
            return True
        if kind in ADDRESS_OPS:
            work.append((parent, depth))
        elif kind == "set_var" and operand == "src":
            work.extend((reader, depth + 1) for reader in graph.readers(parent))
    return False


def encodes(starts: Iterable[Hashable], graph: UseGraph, max_depth: int = MAX_DEPTH) -> bool:
    """Whether a loaded value (the load nodes `starts`) is a key: through width changes, copies, merges and
    operations with constants, it passes an encoding operation (ENCODING_OPS) and then is combined with a
    variable, or returned, stored or passed. Only compared, it is a setting ((K ^ c) == d is K == c ^ d);
    without an encoding operation, a setting, a mask or a pointer."""
    work = [(node, 0, False) for node in starts]
    seen: set = set()
    while work:
        node, depth, encoded = work.pop()
        if (node, encoded) in seen:
            continue
        seen.add((node, encoded))
        if graph.kind(node) == "phi":
            if depth < max_depth:
                work.extend((reader, depth + 1, encoded) for reader in graph.readers(node))
            continue
        up = graph.parent(node)
        if up is None:
            continue
        parent, operand = up
        parent_kind = graph.kind(parent)
        if parent_kind in _VALUE_OPS:
            encoded = encoded or parent_kind in ENCODING_OPS
            if encoded and any(graph.kind(child) != "const" for _, child in graph.operands(parent) if child != node):
                return True
            work.append((parent, depth, encoded))
        elif parent_kind == "set_var" and operand == "src":
            if depth < max_depth:
                work.extend((reader, depth + 1, encoded) for reader in graph.readers(parent))
        elif encoded and (parent_kind, operand) in _MATERIAL:
            return True
    return False
