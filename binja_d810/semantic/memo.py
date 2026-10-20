"""Verdicts remembered by shape, process-wide.

A proof's verdict cannot change when variables of the same width are renamed, so PROVEN and REFUTED
outcomes are kept under ir.expr.shape_key: a predicate repeated with other variables (one large
function repeats one hundreds of times) is proven once. Inconclusive outcomes depend on time and are never
kept here. Keys hold no Binary Ninja references (shape_key rebuilds the leaves).
"""

from __future__ import annotations

_LIMIT = 50_000
_MEMO: dict = {}
MISSING = object()


def get(key):
    return _MEMO.get(key, MISSING)


def put(key, value) -> None:
    if len(_MEMO) >= _LIMIT:
        _MEMO.clear()
    _MEMO[key] = value


def clear() -> None:
    _MEMO.clear()
