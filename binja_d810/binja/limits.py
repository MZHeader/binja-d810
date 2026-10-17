"""Room for D-810's budget in Binary Ninja's own per-function analysis limit (v3.3).

During analysis, Binary Ninja defers a function whose analysis takes longer than
analysis.limits.maxFunctionAnalysisTime (20 s by default): no HLIL. It applies the limit a binary had
when its analysis started; a later change of the setting, on the binary or the function, does not
apply in that session (measured: 40 s set afterwards still deferred tigress_vm's 0x401159 at 20 s).

So, when a binary is finalized, before its analysis starts:
- with D-810 enabled for it, its limit is raised to twice D-810's budget (unless higher, or 0: none);
- the limit it is analyzed with is recorded, and D-810's budget is at most half of it
  (budget.plugin_allowance), whatever the setting says later.
"""

from __future__ import annotations

import threading

from binaryninja import Settings
from binaryninja.log import log_info

from . import settings
from .workflow import ACTIVITY_SETTING

LIMIT = "analysis.limits.maxFunctionAnalysisTime"
_IN_EFFECT: dict = {}  # (file session, view type) -> the limit (ms) Binary Ninja analyzes the view with
_LOCK = threading.Lock()


def room_ms(function_seconds: float) -> int:
    """The limit that leaves the budget its whole share: twice the budget."""
    return int(2000 * function_seconds)


def on_view_finalized(bv) -> None:
    """Binary Ninja's view-finalized event: the binary's analysis has not started yet."""
    s = Settings()
    if s.get_bool(ACTIVITY_SETTING, bv) and s.get_bool(settings.ENABLED, bv):
        current, need = s.get_integer(LIMIT, bv), room_ms(s.get_integer(settings.FUNCTION_SECONDS, bv))
        if 0 < current < need:
            s.set_integer(LIMIT, need, bv)
            log_info(f"binja-d810: {LIMIT} raised from {current / 1000:g} s to {need / 1000:g} s for this binary "
                     f"(twice D-810's budget; Binary Ninja defers a function that takes longer)")
    with _LOCK:
        _IN_EFFECT[_key(bv)] = s.get_integer(LIMIT, bv)


def in_effect_ms(func, current_ms: int) -> int:
    """The limit Binary Ninja applies to `func`'s analysis: the lower of the one its binary was finalized
    with and the setting now (`current_ms`); 0 if neither sets one."""
    with _LOCK:
        recorded = _IN_EFFECT.get(_key(func.view))
    limits = [ms for ms in (recorded, current_ms) if ms is not None and ms > 0]
    return min(limits) if limits else 0


def _key(bv) -> tuple:
    return bv.file.session_id, bv.view_type


def opening_options(function_seconds: float) -> dict:
    """Load options that open a binary with room for a `function_seconds` budget, as if D-810 were
    enabled for it (harnesses that enable D-810 one function at a time): {} if the limit has room."""
    current = Settings().get_integer(LIMIT)
    return {LIMIT: room_ms(function_seconds)} if 0 < current < room_ms(function_seconds) else {}
