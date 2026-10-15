"""Per-function reports: Binary Ninja log output plus the latest report per function."""

from __future__ import annotations

import threading
from typing import Optional

from binaryninja.log import Logger

from .engine import FunctionReport

logger = Logger(0, "binja_d810")
_LOCK = threading.Lock()
_LATEST: dict[tuple[int, int, str], FunctionReport] = {}


def _key(func, level: str) -> tuple[int, int, str]:
    # The view's session, not its file name: two open views of one file must not share reports.
    return (func.view.file.session_id, func.start, level)


def forget(func, level: str) -> None:
    """Drop a pass's report when it runs disabled, so no stale report outlives the setting."""
    with _LOCK:
        _LATEST.pop(_key(func, level), None)


def record(func, report: FunctionReport, level: str = "mlil") -> None:
    with _LOCK:
        _LATEST[_key(func, level)] = report
    for r in report.rewrites:
        why = f" ({r.reason})" if r.reason else ""
        logger.log_info(f"{func.start:#x} @ {r.address:#x} [{r.level} {r.source}] {r.verdict.lower()} by {r.method}{why}: "
                        f"{r.before}  ->  {r.after}")
    for r in report.skipped:
        logger.log_debug(f"{func.start:#x} @ {r.address:#x} [{r.level}] skipped: {r.reason}")
    for r in report.errors:
        logger.log_error(f"{func.start:#x} [{r.level} {r.source}] {r.message}")
    if report.capped:
        logger.log_warn(f"{func.start:#x}: iteration cap reached; still firing: {report.still_firing}")
    if report.rolled_back:
        logger.log_error(f"{func.start:#x}: {report.rolled_back_rewrites} rewrites rolled back; original IL kept")
    if report.budget_exhausted:
        logger.log_warn(f"{func.start:#x}: {level} pass stopped: time budget exhausted "
                        f"({report.plan_seconds:.2f} s planning, overrun {report.overrun_seconds:.2f} s)")


def latest_report(func, level: str = "mlil") -> Optional[FunctionReport]:
    with _LOCK:
        return _LATEST.get(_key(func, level))


def latest_reports(func) -> dict[str, FunctionReport]:
    """The latest report of each pass ("mlil", "hlil") that ran on the function."""
    with _LOCK:
        return {level: r for level in ("mlil", "hlil") if (r := _LATEST.get(_key(func, level))) is not None}


def render(report: FunctionReport) -> str:
    assurance = report.assurance()
    lines = [f"rewrites: {len(report.rewrites)} (proven {assurance['PROVEN']}, tested only {assurance['TESTED']})"
             f"  refuted: {report.refuted}  skipped: {len(report.skipped)}  errors: {len(report.errors)}"]
    for r in report.rewrites:
        why = f", {r.reason}" if r.reason else ""
        lines.append(f"{r.address:#x} [{r.level} {r.source}] {r.verdict.lower()} by {r.method}{why}\n    {r.before}\n -> {r.after}")
    lines += [f"{r.address:#x} [{r.level}] skipped: {r.reason}" for r in report.skipped]
    lines += [f"error [{r.level} {r.source}]: {r.message}" for r in report.errors]
    if report.capped:
        lines.append(f"iteration cap reached; still firing: {report.still_firing}")
    if report.rolled_back:
        lines.append(f"rolled back: {report.rolled_back_rewrites} rewrites undone, original IL kept")
    lines.append(f"time: {report.total_seconds:.3f} s (planning {report.plan_seconds:.3f} s of "
                 f"{report.allowance_seconds:.3f} s allowed, commit {report.commit_seconds:.3f} s, "
                 f"solver lock wait {report.lock_wait_seconds:.3f} s)"
                 + ("; budget exhausted" if report.budget_exhausted else "")
                 + (f"; overran by {report.overrun_seconds:.3f} s" if report.overrun_seconds > 0 else ""))
    return "\n".join(lines)
