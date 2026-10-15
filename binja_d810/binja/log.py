"""Per-function reports: Binary Ninja log output plus the latest report per function."""

from __future__ import annotations

import threading
from typing import Optional

from binaryninja.log import Logger

from .engine import FunctionReport

logger = Logger(0, "binja_d810")
_LOCK = threading.Lock()
_LATEST: dict[tuple[str, int], FunctionReport] = {}


def _key(func) -> tuple[str, int]:
    return (func.view.file.filename, func.start)


def record(func, report: FunctionReport) -> None:
    with _LOCK:
        _LATEST[_key(func)] = report
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


def latest_report(func) -> Optional[FunctionReport]:
    with _LOCK:
        return _LATEST.get(_key(func))


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
    return "\n".join(lines)
