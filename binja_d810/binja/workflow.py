"""Registers the D-810 MLIL activity on Binary Ninja's function workflows."""

import json
from functools import lru_cache
from typing import Optional

from binaryninja import Activity, AnalysisContext, Workflow, log_info

from ..rules.registry import load_rules
from ..semantic.backend import default_backends
from . import log
from .engine import EngineConfig, simplify_function

ACTIVITY_NAME = "extension.binja_d810.mba"
# Key of the enable setting Binary Ninja generates from eligibility.auto (per view / per function).
# Confirmed by tests/binja/test_environment.py::test_auto_setting_identifier.
ACTIVITY_SETTING = ACTIVITY_NAME
BASE_WORKFLOWS = ("core.function.metaAnalysis", "core.function.objectiveC")

_ACTIVITIES: list[Activity] = []  # keep callbacks alive
_rules_override: Optional[list] = None


def set_rules_override(rules: Optional[list]) -> None:
    """Testing hook: replace the Tier 1 rule list (None restores the registry)."""
    global _rules_override
    _rules_override = rules


@lru_cache(maxsize=8)
def _rules(disabled: frozenset[str]) -> tuple:
    return tuple(load_rules(disabled))


@lru_cache(maxsize=1)
def _backends() -> tuple:
    return tuple(default_backends())


def run_activity(ctx: AnalysisContext) -> None:
    try:
        func, mlil = ctx.function, ctx.mlil
        if func is None or mlil is None:
            return
        config = EngineConfig()
        rules = _rules_override if _rules_override is not None else _rules(config.disabled_rules)
        report = simplify_function(mlil, rules, _backends() if config.tier2 else (), config)
        log.record(func, report)
        if report.rewrites:
            ctx.mlil = mlil
    except Exception as exc:  # never break analysis
        log.logger.log_error(f"binja-d810 activity failed: {exc!r}")


def register(action=None) -> list[str]:
    action = action or run_activity
    config = json.dumps({
        "name": ACTIVITY_NAME,
        "title": "D-810: simplify MBA expressions",
        "description": "Rewrites mixed boolean-arithmetic obfuscation in MLIL (binja-d810).",
        "eligibility": {"auto": {"default": False}},
    })
    registered = []
    for base in BASE_WORKFLOWS:
        if base not in Workflow:
            continue
        activity = Activity(configuration=config, action=action)
        _ACTIVITIES.append(activity)
        wf = Workflow(base).clone(base)
        wf.register_activity(activity)
        wf.insert_after("core.function.generateMediumLevelIL", [ACTIVITY_NAME])
        wf.register()
        registered.append(base)
    log_info(f"binja-d810: activity {ACTIVITY_NAME} registered on {registered}")
    return registered
