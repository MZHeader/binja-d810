"""Registers the D-810 activities on Binary Ninja's function workflows.

Two passes, one toggle (spec section 4): the MLIL activity runs after MLIL generation and is the one
users enable ("D-810: simplify MBA expressions"); the HLIL activity runs after HLIL generation for
every function and returns at once unless that same setting is on. It sees HLIL generated from the
already rewritten MLIL, so it only acts on what the MLIL pass could not express.
"""

import ctypes
import json
from functools import lru_cache
from typing import Optional

from binaryninja import Activity, AnalysisContext, Workflow, log_info
from binaryninja.interaction import show_plain_text_report

from ..rules.registry import load_rules
from ..semantic.backend import default_backends
from . import budget, log, settings
from .engine import simplify_function
from .hlil.adapter import HLILAdapter
from .mlil.adapter import MLILAdapter

ACTIVITY_NAME = "extension.binja_d810.mba"
# Key of the enable setting Binary Ninja generates from eligibility.auto (per view / per function).
# Confirmed by tests/binja/test_environment.py::test_auto_setting_identifier.
ACTIVITY_SETTING = ACTIVITY_NAME
HLIL_ACTIVITY_NAME = "extension.binja_d810.mba_hlil"
BASE_WORKFLOWS = ("core.function.metaAnalysis", "core.function.objectiveC")

_ACTIVITIES: list[Activity] = []  # keep callbacks alive
_rules_override: Optional[list] = None
_levels_override: Optional[frozenset] = None
_backends_override: Optional[list] = None


def set_rules_override(rules: Optional[list]) -> None:
    """Testing hook: replace the Tier 1 rule list (None restores the registry)."""
    global _rules_override
    _rules_override = rules


def set_backends_override(backends: Optional[list]) -> None:
    """Testing hook: replace the Tier 2 backends (None restores the defaults)."""
    global _backends_override
    _backends_override = backends


def set_levels_override(levels: Optional[set]) -> None:
    """Testing hook: run only these passes ({"mlil"}, {"hlil"}); None runs both."""
    global _levels_override
    _levels_override = None if levels is None else frozenset(levels)


@lru_cache(maxsize=8)
def _rules(disabled: frozenset[str]) -> tuple:
    return tuple(load_rules(disabled))


@lru_cache(maxsize=1)
def _backends() -> tuple:
    return tuple(default_backends())


def _enabled(ctx) -> bool:
    func = ctx.function
    return (func is not None and ctx.get_setting_bool(ACTIVITY_SETTING) and ctx.get_setting_bool(settings.ENABLED)
            and func.start not in settings.skipped_functions(ctx))


def _analysis_limit_ms(ctx) -> int:
    try:
        return ctx.get_setting_uint64("analysis.limits.maxFunctionAnalysisTime")
    except Exception:
        return 20_000


def _handle(il) -> int:
    return ctypes.addressof(il.handle.contents)


def _run(ctx, level: str) -> None:
    if _levels_override is not None and level not in _levels_override:
        return
    func = ctx.function
    il = ctx.mlil if level == "mlil" else ctx.hlil
    if il is None:
        return
    config = settings.engine_config(ctx)
    rules = _rules_override if _rules_override is not None else _rules(config.disabled_rules)
    # One allowance per function and analysis run, shared by both passes: the generation is the
    # MLIL the MLIL pass produced, which the HLIL activity sees as ctx.mlil (binja/budget.py).
    mlil = ctx.mlil
    allowance = budget.LEDGER.begin((func.view.file.session_id, func.start), level,
                                    generation=_handle(mlil) if mlil is not None else _handle(il),
                                    keepalive=mlil if mlil is not None else il,
                                    allowance=budget.plugin_allowance(config.function_seconds, _analysis_limit_ms(ctx)))
    make = MLILAdapter if level == "mlil" else HLILAdapter
    adapter = make(il, config.limits.max_nodes, allowance.deadline)
    backends = _backends_override if _backends_override is not None else _backends()
    report = simplify_function(adapter, rules, backends if config.tier2 else (), config, allowance.deadline)
    allowance.charge(report.total_seconds)
    log.record(func, report, level)
    if report.rewrites:
        if level == "mlil":
            ctx.mlil = il
        else:
            ctx.hlil = il


def run_activity(ctx: AnalysisContext) -> None:
    try:
        if _enabled(ctx):
            _run(ctx, "mlil")
        elif ctx.function is not None:
            log.forget(ctx.function, "mlil")
    except Exception as exc:  # never break analysis
        log.logger.log_error(f"binja-d810 MLIL activity failed: {exc!r}")


def run_hlil_activity(ctx: AnalysisContext) -> None:
    try:
        if not _enabled(ctx):
            if ctx.function is not None:
                log.forget(ctx.function, "hlil")
            return
        _run(ctx, "hlil")
        func = ctx.function
        if func.check_for_debug_report("d810"):
            reports = log.latest_reports(func)
            text = "\n\n".join(f"== {level.upper()} pass\n{log.render(r)}" for level, r in reports.items())
            show_plain_text_report(f"D-810: {func.name}", text)
    except Exception as exc:  # never break analysis
        log.logger.log_error(f"binja-d810 HLIL activity failed: {exc!r}")


def register(action=None, hlil_action=None) -> list[str]:
    action = action or run_activity
    hlil_action = hlil_action or run_hlil_activity
    config = json.dumps({
        "name": ACTIVITY_NAME,
        "title": "D-810: simplify MBA expressions",
        "description": "Rewrites mixed boolean-arithmetic obfuscation in MLIL, then HLIL (binja-d810).",
        "eligibility": {"auto": {"default": False}},
    })
    # No eligibility of its own: it follows ACTIVITY_SETTING (one toggle for both passes).
    hlil_config = json.dumps({
        "name": HLIL_ACTIVITY_NAME,
        "title": "D-810: simplify MBA expressions (HLIL pass)",
        "description": "Rewrites in HLIL what the MLIL pass could not; enabled by the D-810 setting.",
    })
    registered = []
    for base in BASE_WORKFLOWS:
        if base not in Workflow:
            continue
        activity = Activity(configuration=config, action=action)
        hlil_activity = Activity(configuration=hlil_config, action=hlil_action)
        _ACTIVITIES.extend([activity, hlil_activity])
        wf = Workflow(base).clone(base)
        wf.register_activity(activity)
        wf.register_activity(hlil_activity)
        wf.insert_after("core.function.generateMediumLevelIL", [ACTIVITY_NAME])
        wf.insert_after("core.function.generateHighLevelIL", [HLIL_ACTIVITY_NAME])
        wf.register()
        registered.append(base)
    log_info(f"binja-d810: activities {ACTIVITY_NAME}, {HLIL_ACTIVITY_NAME} registered on {registered}")
    return registered
