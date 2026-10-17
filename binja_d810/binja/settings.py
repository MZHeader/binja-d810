"""Binary Ninja settings for D-810. Per-function enabling is the activity's own eligibility setting."""

from __future__ import annotations

import json

from binaryninja import Settings

from ..simplify import Limits
from .engine import EngineConfig

PREFIX = "binja_d810"
ENABLED = f"{PREFIX}.enabled"
TIER2 = f"{PREFIX}.tier2"
MAX_VARS = f"{PREFIX}.maxVariables"
MAX_NODES = f"{PREFIX}.maxNodes"
MAX_ITERATIONS = f"{PREFIX}.maxIterations"
DISABLED_RULES = f"{PREFIX}.disabledRules"
SKIP_FUNCTIONS = f"{PREFIX}.skipFunctions"
FUNCTION_SECONDS = f"{PREFIX}.functionSeconds"

_SPECS = {
    ENABLED: {"title": "Enabled", "type": "boolean", "default": True,
              "description": "Global kill switch. When off, D-810 never rewrites anything."},
    TIER2: {"title": "Generic MBA simplifier (Tier 2)", "type": "boolean", "default": True,
            "description": "Use CoBRA (if installed) and the built-in linear-MBA simplifier."},
    MAX_VARS: {"title": "Tier 2 variable limit", "type": "number", "default": 4, "minValue": 1, "maxValue": 6,
               "description": "CoBRA skips subexpressions with more distinct variables (the linear simplifier counts atoms instead, up to 6)."},
    MAX_NODES: {"title": "Expression node limit", "type": "number", "default": 200, "minValue": 10, "maxValue": 5000,
                "description": "Expressions larger than this are skipped and logged."},
    MAX_ITERATIONS: {"title": "Iteration cap", "type": "number", "default": 10, "minValue": 1, "maxValue": 100,
                     "description": "Maximum rewrite passes per function."},
    DISABLED_RULES: {"title": "Disabled rules", "type": "array", "elementType": "string", "default": [],
                     "description": "Tier 1 rule names to skip."},
    SKIP_FUNCTIONS: {"title": "Skipped functions", "type": "array", "elementType": "string", "default": [],
                     "description": "Function start addresses (hex) that D-810 never rewrites."},
    FUNCTION_SECONDS: {"title": "Time budget per function (seconds)", "type": "number", "default": 10,
                       "minValue": 1, "maxValue": 600,
                       "description": "Planning and solving time shared by the MLIL and HLIL passes of one "
                                      "analysis run. When it runs out, already verified rewrites are applied "
                                      "and nothing new is attempted. At most half of "
                                      "analysis.limits.maxFunctionAnalysisTime (10 s at its default)."},
}


def register() -> None:
    s = Settings()
    s.register_group(PREFIX, "D-810")
    for key, spec in _SPECS.items():
        s.register_setting(key, json.dumps(spec))


def engine_config(ctx) -> EngineConfig:
    # The analysis context caches "number" settings with integer defaults as uint64;
    # get_setting_double fails with "SettingsCache failed to get a cached setting". Set them
    # with Settings().set_integer: a value stored with set_double is ignored by integer reads.
    return EngineConfig(
        limits=Limits(max_vars=ctx.get_setting_uint64(MAX_VARS), max_nodes=ctx.get_setting_uint64(MAX_NODES)),
        max_iterations=ctx.get_setting_uint64(MAX_ITERATIONS),
        tier2=ctx.get_setting_bool(TIER2),
        disabled_rules=frozenset(ctx.get_setting_string_list(DISABLED_RULES)),
        function_seconds=float(ctx.get_setting_uint64(FUNCTION_SECONDS)),
    )


def skipped_functions(ctx) -> set[int]:
    out = set()
    for text in ctx.get_setting_string_list(SKIP_FUNCTIONS):
        try:
            out.add(int(text, 16))
        except ValueError:
            continue
    return out
