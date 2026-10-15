"""Liveness policy shared by both IL levels (spec section 5.4).

An adapter describes its function as pure definitions (key -> the keys its value reads) plus the
keys read by everything else (effects, returns, branches, impure definitions). A pure definition
is dead if no live use reaches it, even through other pure definitions. The engine removes only
the definitions its own rewrites made dead: dead_after - dead_before.
"""

from __future__ import annotations

from typing import Hashable, Iterable, Mapping


def dead_definitions(pure: Mapping[Hashable, Iterable[Hashable]], roots: Iterable[Hashable]) -> set[Hashable]:
    live: set = set()
    work = list(roots)
    while work:
        key = work.pop()
        if key in live:
            continue
        live.add(key)
        work.extend(pure.get(key, ()))
    return {key for key in pure if key not in live}
