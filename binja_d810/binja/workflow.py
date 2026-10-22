"""Registers the D-810 activities on Binary Ninja's function workflows.

Two passes, one toggle: the MLIL activity runs after MLIL generation and is the one
users enable ("D-810: simplify MBA expressions"); the HLIL activity runs after HLIL generation for
every function and returns at once unless that same setting is on. It sees HLIL generated from the
already rewritten MLIL, so it only acts on what the MLIL pass could not express. Unflattening (v4.2)
follows the same toggle in an activity of its own, after Binary Ninja resolves indirect branches.
"""

import ctypes
import json
import time
from functools import lru_cache
from typing import Optional

from binaryninja import Activity, AnalysisContext, Workflow, log_info
from binaryninja.interaction import show_plain_text_report

from ..deadline import Deadline
from ..flow.ranges import parse as parse_ranges
from ..rules.registry import load_rules
from ..semantic.backend import default_backends
from . import budget, log, settings, unflatten
from .engine import simplify_function
from .flow import apply as flow_apply, branches
from .hlil.adapter import HLILAdapter
from .mlil.adapter import MLILAdapter

ACTIVITY_NAME = "extension.binja_d810.mba"
# Key of the enable setting Binary Ninja generates from eligibility.auto (per view / per function).
# Confirmed by tests/binja/test_environment.py::test_auto_setting_identifier.
ACTIVITY_SETTING = ACTIVITY_NAME
HLIL_ACTIVITY_NAME = "extension.binja_d810.mba_hlil"
BRANCHES_ACTIVITY_NAME = "extension.binja_d810.branches"
BRANCH_RESET = "core.function.resetIndirectBranchesOnFullUpdate"
UNFLATTEN_ACTIVITY_NAME = "extension.binja_d810.unflatten"
# That analysis reads ctx.mlil: a rewrite before it removes the jump it resolves, and the function's
# analysis then never settles (v4.2, measured). Without it, unflattening follows the MLIL pass.
UNFLATTEN_AFTER = "core.function.analyzeIndirectBranches"
BASE_WORKFLOWS = ("core.function.metaAnalysis", "core.function.objectiveC")
DISPATCH_RESERVE_SECONDS = 10.0

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


def _flow(ctx, adapter, func, report) -> bool:
    """The flow fixes (v4.0), after the MLIL pass. True when they changed the MLIL."""
    try:
        enabled = ctx.get_setting_bool(settings.FLOW_FIXES)
        flow_apply.observe_enabled(func, enabled, ctx.get_setting_string_list(settings.READ_ONLY_RANGES))
        if not enabled:
            flow_apply.release(func.view, func.start)
            return False
        return flow_apply.after_pass(func.view, func, adapter, report)
    except Exception as exc:  # never break analysis
        from . import completion
        completion.failed(func.view)
        log.logger.log_error(f"binja-d810 flow fixes failed at {func.start:#x}: {exc!r}")
        return False


def _begin(ctx, config, level: str, il) -> budget.Allowance:
    # One allowance per function and analysis run, shared by all passes: the generation is the
    # MLIL the MLIL pass produced, which the later activities see as ctx.mlil (binja/budget.py).
    func, mlil = ctx.function, ctx.mlil
    from . import limits  # it imports this module

    # The limit Binary Ninja really applies: the one the binary was opened with (v3.3, measured).
    limit_ms = limits.in_effect_ms(func, _analysis_limit_ms(ctx))
    return budget.LEDGER.begin((func.view.file.session_id, func.start), level,
                               generation=_handle(mlil) if mlil is not None else _handle(il),
                               keepalive=mlil if mlil is not None else il,
                               allowance=budget.plugin_allowance(config.function_seconds, limit_ms))


def _run(ctx, level: str) -> None:
    from . import completion
    if _levels_override is not None and level not in _levels_override:
        return
    func = ctx.function
    il = ctx.mlil if level == "mlil" else ctx.hlil
    if il is None:
        return
    completion.touch(func.view)
    config = settings.engine_config(ctx)
    rules = _rules_override if _rules_override is not None else _rules(config.disabled_rules)
    allowance = _begin(ctx, config, level, il)
    reserved = probe_seconds = probe_cpu = 0.0
    if level == "mlil" and ctx.get_setting_bool(settings.UNFLATTEN) and allowance.remaining() >= 2:
        started, cpu = time.monotonic(), time.thread_time()
        remaining = allowance.remaining()
        ranges, _ = parse_ranges(ctx.get_setting_string_list(settings.READ_ONLY_RANGES))
        probe_deadline = allowance.deadline.earlier(Deadline.after(min(1.0, remaining / 20)))
        if unflatten.has_table_dispatcher(il, func.view, deadline=probe_deadline, ranges=ranges):
            # Recovery and the resulting HLIL both need a turn. One slow backend call can overrun
            # the MLIL deadline, so a small tail allowance alone can still starve both later passes.
            reserved = min(DISPATCH_RESERVE_SECONDS, remaining / 2)
        probe_seconds, probe_cpu = time.monotonic() - started, time.thread_time() - cpu
    deadline = Deadline(allowance.deadline.at - reserved)
    make = MLILAdapter if level == "mlil" else HLILAdapter
    adapter = make(il, config.limits.max_nodes, deadline)
    backends = _backends_override if _backends_override is not None else _backends()
    report = simplify_function(adapter, rules, backends if config.tier2 else (), config, deadline)
    report.reserved_seconds = reserved
    report.dispatch_probe_seconds = probe_seconds
    report.allowance_seconds += probe_seconds
    report.total_seconds += probe_seconds
    report.plan_seconds += probe_seconds
    report.plan_cpu_seconds += probe_cpu
    changed = _flow(ctx, adapter, func, report) if level == "mlil" else False
    allowance.charge(report.total_seconds)
    log.record(func, report, level)
    if report.errors or report.rolled_back:
        completion.failed(func.view)
    if report.rewrites or changed:
        if level == "mlil":
            ctx.mlil = il
        else:
            ctx.hlil = il


def _unflatten(ctx) -> None:
    """Unflattening (v4.2): the MLIL pass's allowance and report, and each round committed to ctx.mlil."""
    func, mlil = ctx.function, ctx.mlil
    if mlil is None or not ctx.get_setting_bool(settings.UNFLATTEN):
        return
    from . import completion
    completion.touch(func.view)
    allowance = _begin(ctx, settings.engine_config(ctx), "mlil", mlil)
    ranges, _ = parse_ranges(ctx.get_setting_string_list(settings.READ_ONLY_RANGES))
    started = time.monotonic()
    try:
        lines = unflatten.run(ctx, deadline=allowance.deadline, ranges=ranges)
    finally:
        allowance.charge(time.monotonic() - started)
        if _handle(ctx.mlil) != _handle(mlil):
            allowance.adopt(_handle(ctx.mlil), ctx.mlil)  # the HLIL pass sees the unflattened MLIL
    report = log.latest_report(func, "mlil")
    if report is not None:
        report.unflatten = lines
    for line in lines:
        log.logger.log_info(f"{func.start:#x} unflatten: {line}")


def debug_report_text(func) -> str:
    """The D-810 debug report: each pass's report, then what the flow fixes did or skipped for the function."""
    reports = log.latest_reports(func)
    text = "\n\n".join(f"== {level.upper()} pass\n{log.render(r)}" for level, r in reports.items())
    notes = flow_apply.notes_for(func.view, func.start)
    if notes:
        text += "\n\n== flow\n" + "\n".join(notes)
    return text


def run_activity(ctx: AnalysisContext) -> None:
    try:
        if _enabled(ctx):
            _run(ctx, "mlil")
        elif ctx.function is not None:
            log.forget(ctx.function, "mlil")
            flow_apply.release(ctx.function.view, ctx.function.start)
    except Exception as exc:  # never break analysis
        from . import completion
        if ctx.function is not None:
            completion.failed(ctx.function.view)
        log.logger.log_error(f"binja-d810 MLIL activity failed: {exc!r}")


def run_hlil_activity(ctx: AnalysisContext) -> None:
    try:
        if not _enabled(ctx):
            if ctx.function is not None:
                log.forget(ctx.function, "hlil")
                flow_apply.observe_enabled(ctx.function, False, [])
                flow_apply.release(ctx.function.view, ctx.function.start)
            return
        _run(ctx, "hlil")
        func = ctx.function
        if func.check_for_debug_report("d810"):
            show_plain_text_report(f"D-810: {func.name}", debug_report_text(func))
    except Exception as exc:  # never break analysis
        from . import completion
        if ctx.function is not None:
            completion.failed(ctx.function.view)
        log.logger.log_error(f"binja-d810 HLIL activity failed: {exc!r}")


def run_unflatten_activity(ctx: AnalysisContext) -> None:
    func = ctx.function
    try:
        if _enabled(ctx) and (_levels_override is None or "mlil" in _levels_override):
            _unflatten(ctx)
    except Exception as exc:  # never break analysis: the last committed MLIL stays
        from . import completion
        if func is not None:
            completion.failed(func.view)
        log.logger.log_error(f"binja-d810 unflatten failed at {func.start:#x}: {exc!r}")


def run_branches_activity(ctx: AnalysisContext) -> None:
    try:
        enabled = _enabled(ctx) and ctx.get_setting_bool(settings.FLOW_FIXES)
        if ctx.function is not None:
            flow_apply.observe_enabled(ctx.function, enabled, ctx.get_setting_string_list(settings.READ_ONLY_RANGES))
            flow_apply.restore_inline(ctx.function)
        if enabled:
            branches.apply_branches(ctx.function)
        elif ctx.function is not None:
            # The MBA activity's eligibility skips its callback entirely when its toggle is off.
            flow_apply.release(ctx.function.view, ctx.function.start)
    except Exception as exc:  # never break analysis
        from . import completion
        if ctx.function is not None:
            completion.failed(ctx.function.view)
        log.logger.log_error(f"binja-d810 branches activity failed: {exc!r}")


def register(action=None, hlil_action=None, branches_action=None, unflatten_action=None) -> list[str]:
    action = action or run_activity
    hlil_action = hlil_action or run_hlil_activity
    branches_action = branches_action or run_branches_activity
    unflatten_action = unflatten_action or run_unflatten_activity
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
    branches_config = json.dumps({
        "name": BRANCHES_ACTIVITY_NAME,
        "title": "D-810: computed jump targets",
        "description": "Restores recorded wrapper inline flags and computed jump targets (binja-d810).",
    })
    unflatten_config = json.dumps({
        "name": UNFLATTEN_ACTIVITY_NAME,
        "title": "D-810: unflatten control flow",
        "description": "Threads flattened control flow past its dispatcher in MLIL; enabled by the D-810 setting.",
    })
    registered = []
    for base in BASE_WORKFLOWS:
        if base not in Workflow:
            continue
        activity = Activity(configuration=config, action=action)
        hlil_activity = Activity(configuration=hlil_config, action=hlil_action)
        branches_activity = Activity(configuration=branches_config, action=branches_action)
        unflatten_activity = Activity(configuration=unflatten_config, action=unflatten_action)
        _ACTIVITIES.extend([activity, hlil_activity, branches_activity, unflatten_activity])
        wf = Workflow(base).clone(base)
        wf.register_activity(activity)
        wf.register_activity(hlil_activity)
        wf.insert_after("core.function.generateMediumLevelIL", [ACTIVITY_NAME])
        wf.insert_after("core.function.generateHighLevelIL", [HLIL_ACTIVITY_NAME])
        wf.register_activity(branches_activity)
        if wf.contains(BRANCH_RESET):
            wf.insert_after(BRANCH_RESET, [BRANCHES_ACTIVITY_NAME])
        wf.register_activity(unflatten_activity)
        wf.insert_after(UNFLATTEN_AFTER if wf.contains(UNFLATTEN_AFTER) else ACTIVITY_NAME, [UNFLATTEN_ACTIVITY_NAME])
        wf.register()
        registered.append(base)
    log_info(f"binja-d810: activities {ACTIVITY_NAME}, {HLIL_ACTIVITY_NAME}, {BRANCHES_ACTIVITY_NAME}, "
             f"{UNFLATTEN_ACTIVITY_NAME} registered on {registered}")
    return registered
