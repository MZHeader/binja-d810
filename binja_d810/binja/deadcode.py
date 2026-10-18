"""Liveness policy shared by both IL levels.

An adapter describes its function as pure definitions (key -> the keys its value reads) plus the
keys read by everything else (effects, returns, branches, impure definitions). A pure definition
is dead if no live use reaches it, even through other pure definitions. The engine removes only
the definitions its own rewrites made dead: dead_after - dead_before.
"""

from __future__ import annotations

from typing import Callable, Hashable, Iterable, Mapping, Optional


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


def dead_with_checks(pure: Mapping[Hashable, Iterable[Hashable]], roots: Iterable[Hashable],
                     checks: Mapping[Hashable, Callable[[], bool]]) -> set[Hashable]:
    """dead_definitions where some definitions are pure only if a check passes (a division that cannot
    trap, v3.5). A check runs only for a definition that would be removed: a live one keeps what it reads
    alive whether or not it is pure. One that fails is not removable and keeps its reads alive."""
    pure, roots, pending = dict(pure), list(roots), dict(checks)
    while True:
        dead = dead_definitions(pure, roots)
        failed = [key for key in dead if key in pending and not pending.pop(key)()]
        if not failed:
            return dead
        for key in failed:
            roots.extend(pure.pop(key))


def dying_with_root(uses: Mapping[Hashable, Iterable[int]], defined_at: Mapping[Hashable, int],
                    root_statement: int) -> set[Hashable]:
    """The inlined definitions a rewrite of the root would leave dead: every use is in the root's
    statement or in another such definition. Used to measure the code a rewrite really replaces."""
    dying: set = set()
    changed = True
    while changed:
        changed = False
        allowed = {root_statement} | {defined_at[d] for d in dying}
        for key, statements in uses.items():
            statements = list(statements)
            if key not in dying and statements and all(s in allowed for s in statements):
                dying.add(key)
                changed = True
    return dying


def shown_in(dying: Iterable[Hashable], uses: Mapping[Hashable, Iterable[int]], defined_at: Mapping[Hashable, int],
             root_statement: int) -> dict[Hashable, Optional[Hashable]]:
    """The statement HLIL shows each dying definition in: one used once is folded into the
    statement that uses it (repeatedly), one used several times is a statement of its own (named
    by itself); None is the root's statement. MLIL is three-address code, so its statements say
    little about what the user reads."""
    dying = set(dying)
    at = {defined_at[key]: key for key in dying}
    shown: dict = {}
    for key in dying:
        current, seen = key, set()
        while current not in seen and len(list(uses[current])) == 1:
            seen.add(current)
            user = list(uses[current])[0]
            if user == root_statement:
                current = None
                break
            if user not in at:
                break
            current = at[user]
        shown[key] = current
    return shown
