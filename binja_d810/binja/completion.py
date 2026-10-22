"""One console completion message after native analysis and D810's queued work settle."""

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from binaryninja import AnalysisState

from . import log


@dataclass
class Pending:
    generation: int = 0
    held: int = 0
    armed: bool = False
    event: object = None
    failed: bool = False


_LOCK = threading.RLock()
_PENDING = {}


def _key(view):
    return getattr(getattr(view, "file", None), "session_id", None)


def touch(view):
    key = _key(view)
    if key is None:
        return
    with _LOCK:
        state = _PENDING.setdefault(key, Pending())
        state.generation += 1
    _arm(view, state)


@contextmanager
def batch(view):
    """A menu action can schedule several functions before announcing completion."""
    key = _key(view)
    with _LOCK:
        state = _PENDING.setdefault(key, Pending())
        state.held += 1
        state.generation += 1
    try:
        yield
    except Exception:
        failed(view)
        raise
    finally:
        with _LOCK:
            state.held -= 1
        _arm(view, state)


def failed(view):
    key = _key(view)
    if key is not None:
        with _LOCK:
            state = _PENDING.setdefault(key, Pending())
            state.failed = True
            state.generation += 1


def _arm(view, state):
    with _LOCK:
        if _PENDING.get(_key(view)) is not state or state.held or state.armed:
            return
        state.armed = True
    try:
        event = view.add_analysis_completion_event(lambda: _native_done(view, state))
    except Exception:  # the view may have closed while its analysis was ending
        with _LOCK:
            _PENDING.pop(_key(view), None)
        return
    with _LOCK:
        if state.armed:  # an already-idle view may have called us synchronously
            state.event = event


def _native_done(view, state):
    with _LOCK:
        if _PENDING.get(_key(view)) is not state:
            return
        state.armed = False
        state.event = None
    done(view)


def done(view):
    """Also called by the flow worker after its final batch, including deferred undo."""
    from .flow import apply

    key = _key(view)
    with _LOCK:
        state = _PENDING.get(key)
        if state is None or state.held:
            return
        generation = state.generation
    if apply.busy(view):
        return  # the flow worker will call done after draining its queue
    try:
        idle = view.analysis_info.state == AnalysisState.IdleState
    except Exception:  # a closed view has no analysis left to announce
        with _LOCK:
            if _PENDING.get(key) is state:
                _PENDING.pop(key)
        return
    if not idle:
        _arm(view, state)
        return
    with _LOCK:
        if _PENDING.get(key) is not state or state.held or state.generation != generation:
            return
        _PENDING.pop(key)
        state.event = None
    name = os.path.basename(view.file.filename).replace("\n", " ") or "this binary"
    status = "analysis finished with errors" if state.failed else "analysis complete"
    log.logger.log_info(f"D810: {status} for {name}.")
