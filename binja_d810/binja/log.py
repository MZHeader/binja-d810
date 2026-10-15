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
    for address, source, before, after in report.rewrites:
        logger.log_info(f"{func.start:#x} @ {address:#x} [{source}] {before}  ->  {after}")
    for address, reason in report.skipped:
        logger.log_debug(f"{func.start:#x} @ {address:#x} skipped: {reason}")
    for source, message in report.errors:
        logger.log_error(f"{func.start:#x} [{source}] {message}")
    if report.capped:
        logger.log_warn(f"{func.start:#x}: iteration cap reached; still firing: {report.still_firing}")
    if report.rolled_back:
        logger.log_error(f"{func.start:#x}: rewrites rolled back; original MLIL kept")


def latest_report(func) -> Optional[FunctionReport]:
    with _LOCK:
        return _LATEST.get(_key(func))


def render(report: FunctionReport) -> str:
    lines = [f"rewrites: {len(report.rewrites)}  skipped: {len(report.skipped)}  errors: {len(report.errors)}"]
    lines += [f"{address:#x} [{source}]\n    {before}\n -> {after}" for address, source, before, after in report.rewrites]
    lines += [f"{address:#x} skipped: {reason}" for address, reason in report.skipped]
    lines += [f"error [{source}]: {message}" for source, message in report.errors]
    if report.capped:
        lines.append(f"iteration cap reached; still firing: {report.still_firing}")
    if report.rolled_back:
        lines.append("rolled back: original MLIL kept")
    return "\n".join(lines)
