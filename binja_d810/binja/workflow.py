"""Registers the D-810 MLIL activity on Binary Ninja's function workflows."""

import json

from binaryninja import Activity, AnalysisContext, Workflow, log_info

ACTIVITY_NAME = "extension.binja_d810.mba"
# Key of the enable setting Binary Ninja generates from eligibility.auto (per view / per function).
# Confirmed by tests/binja/test_environment.py::test_auto_setting_identifier.
ACTIVITY_SETTING = ACTIVITY_NAME
BASE_WORKFLOWS = ("core.function.metaAnalysis", "core.function.objectiveC")

_ACTIVITIES: list[Activity] = []  # keep callbacks alive

# Spike probe: filled by _probe_action, read by tests/binja/test_environment.py. Removed in Task 17.
PROBE: dict[int, dict] = {}


def _probe_action(ctx: AnalysisContext) -> None:
    mlil = ctx.mlil
    PROBE[ctx.function.start] = {
        "has_mlil": mlil is not None,
        "has_ssa": mlil is not None and mlil.ssa_form is not None,
    }


def register(action=_probe_action) -> list[str]:
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
