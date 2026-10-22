"""Early callee substitution; its time is charged to the later shared MLIL allowance."""

import threading
import time
from dataclasses import dataclass, field

from ...deadline import Deadline
from .. import budget, completion, log, settings
from ..flow import apply
from .rewrite import plan, rewrite
from . import native


@dataclass
class Report:
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    exhausted: bool = False
    calls: int = 0


_LOCK = threading.Lock()
_LATEST = {}
_RETRIED = set()


def take(func):
    with _LOCK:
        return _LATEST.pop((func.view.file.session_id, func.start), None)


def run(ctx):
    from .. import limits, workflow

    func = ctx.function
    if func is None:
        return
    started, cpu = time.monotonic(), time.thread_time()
    key = (func.view.file.session_id, func.start)
    enabled = (workflow._enabled(ctx) and ctx.get_setting_bool(settings.CALLEES)
               and (workflow._levels_override is None or "mlil" in workflow._levels_override))
    native_enabled = enabled and ctx.get_setting_bool(settings.CALLEE_NATIVE) and ctx.get_setting_bool(settings.FLOW_FIXES)
    native.observe(func, native_enabled)
    if not enabled:
        with _LOCK:
            _LATEST.pop(key, None)
            _RETRIED.discard(key)
        return
    il = ctx.lifted_il
    if il is None:
        return
    report = Report()
    completion.touch(func.view)
    config = settings.callee_limits(ctx)
    seconds = budget.plugin_allowance(ctx.get_setting_uint64(settings.FUNCTION_SECONDS),
                                      limits.in_effect_ms(func, workflow._analysis_limit_ms(ctx)))
    deadline = Deadline.after(seconds)
    try:
        sites, report.notes = plan(il, func, config, deadline)
        if sites:
            new = rewrite(il, sites, config, deadline)
            ctx.lifted_il = new
            report.calls = len(sites)
            report.notes.insert(0, f"{len(sites)} call sites substituted; proven by exact IL copies preserving register and flag effects")
            for site in sites.values():
                if site.summary.function.inline_during_analysis.value:
                    report.notes.append(f"native inline flag left call at {il[site.index].address:#x}; used checked substitution")
            if native_enabled:
                native.request(func, sites, config)
        if any("lifted IL unavailable" in line for line in report.notes):
            with _LOCK:
                retry = key not in _RETRIED
                _RETRIED.add(key)
            if retry:
                apply.defer_reanalysis(func)
    except (ValueError, NotImplementedError) as exc:
        report.notes.append(f"left unchanged: {exc}")
    except Exception as exc:
        # The new IL is assigned only after every copied operation has passed its exact check.
        report.notes.append(f"left unchanged: {exc!r}")
        completion.failed(func.view)
        log.logger.log_error(f"{func.start:#x} callee summaries failed: {exc!r}")
    finally:
        report.seconds, report.cpu_seconds = time.monotonic() - started, time.thread_time() - cpu
        report.exhausted = deadline.expired()
        with _LOCK:
            _LATEST[key] = report
