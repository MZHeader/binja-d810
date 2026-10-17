"""Per-path variants of an expression over merged values (spec v3.2 goal 3).

A merged value (an SSA phi) takes, when control enters its block along an edge, its incoming value
for that edge, and every merged value of the block does so at the same moment. A variant replaces
the merged values of one block together by their incoming values along one edge, so their relation
on that path is kept ((y > 9, y < 10) on one edge, (1, 0) on the other).

The incoming values' own leaves are renamed apart for each edge: in a loop, the incoming value along
the back edge was computed in an earlier iteration, and must not be identified with the current value
of the same SSA variable (p = phi(a, L + 1) compared with L + 1 is not constant). Renaming only adds
freedom, so a variant proven constant holds on its path, and when every variant is the same proven
constant, so is the expression.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Hashable, Optional

from .expr import BinOp, Const, Expr, UnOp, Var, nodes

_TAG = "~"  # leaf name suffix: the path a renamed leaf belongs to (never in Binary Ninja names)


@dataclass(frozen=True)
class Merge:
    """A merged value: its block (merges of one block share their incoming edges) and its incoming
    value along each edge, in the block's edge order (leaves keep their references)."""

    block: Hashable
    incoming: tuple[Expr, ...]


def path_variants(e: Expr, merge_of: Callable[[Var], Optional[Merge]], *, depth: int = 2,
                  limit: int = 16) -> Optional[list[Expr]]:
    """`e` once per combination of incoming edges of the merges it reads, `depth` merges deep; None
    if it reads no merged value or the variants would exceed `limit`."""
    variants, split = [e], False
    for _ in range(depth):
        out, changed = [], False
        for variant in variants:
            groups = _groups(variant, merge_of)
            if not groups:
                out.append(variant)
                continue
            changed = True
            keys = list(groups)
            edges = [len(next(iter(groups[key].values())).incoming) for key in keys]
            count = 1
            for n in edges:
                count *= n
            if len(out) + count > limit:
                return None
            for combo in itertools.product(*(range(n) for n in edges)):
                mapping: dict[tuple[str, int], Expr] = {}
                for (block, tag), edge in zip(keys, combo):
                    path = f"{tag}/{block}.{edge}"
                    for leaf, merge in groups[(block, tag)].items():
                        mapping[(leaf.name, leaf.width)] = _renamed(merge.incoming[edge], path)
                out.append(_substitute(variant, mapping))
        if not changed:
            break
        split, variants = True, out
    return variants if split else None


def _groups(e: Expr, merge_of: Callable[[Var], Optional[Merge]]) -> dict:
    """Merged leaves by (block, path tag): the same block reached along the same path."""
    groups: dict[tuple, dict[Var, Merge]] = {}
    for node in nodes(e):
        if not isinstance(node, Var):
            continue
        merge = merge_of(node)
        if merge is None or not merge.incoming:
            continue
        tag = node.name.split(_TAG, 1)[1] if _TAG in node.name else ""
        group = groups.setdefault((merge.block, tag), {})
        if group and len(next(iter(group.values())).incoming) != len(merge.incoming):
            return {}  # merges of one block always share their edges; anything else is not split
        group[node] = merge
    return groups


def _renamed(e: Expr, path: str) -> Expr:
    """`e` with each leaf renamed for `path` (references kept, so a merge inside can be split)."""
    mapping = {}
    for node in nodes(e):
        if isinstance(node, Var):
            base = node.name.split(_TAG, 1)[0]
            mapping[(node.name, node.width)] = Var(f"{base}{_TAG}{path}", node.width, ref=node.ref)
    return _substitute(e, mapping)


def _substitute(e: Expr, mapping: dict[tuple[str, int], Expr]) -> Expr:
    """Leaves replaced by (name, width); each shared value rebuilt once and kept shared."""
    built: dict[int, Expr] = {}
    stack = [(e, False)]
    while stack:
        node, ready = stack.pop()
        if id(node) in built:
            continue
        if isinstance(node, Var):
            built[id(node)] = mapping.get((node.name, node.width), node)
        elif isinstance(node, Const):
            built[id(node)] = node
        elif not ready:
            stack.append((node, True))
            stack.extend((k, False) for k in ((node.operand,) if isinstance(node, UnOp) else (node.left, node.right))
                         if id(k) not in built)
        elif isinstance(node, UnOp):
            built[id(node)] = UnOp(node.op, built[id(node.operand)], node.width)
        else:
            built[id(node)] = BinOp(node.op, built[id(node.left)], built[id(node.right)], node.width)
    return built[id(e)]
