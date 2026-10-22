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
FLOW_FIXES = f"{PREFIX}.flowFixes"
READ_ONLY_RANGES = f"{PREFIX}.readOnlyRanges"
UNFLATTEN = f"{PREFIX}.unflatten"
CALLEES = f"{PREFIX}.calleeSummaries"
CALLEE_INSTRUCTIONS = f"{PREFIX}.calleeInstructions"
CALLEE_NODES = f"{PREFIX}.calleeNodes"
CALLEE_FRAME_BYTES = f"{PREFIX}.calleeFrameBytes"
CALLEE_CALLER_INSTRUCTIONS = f"{PREFIX}.calleeCallerInstructions"
CALLEE_SITES = f"{PREFIX}.calleeSites"
CALLEE_PROOF_MS = f"{PREFIX}.calleeProofMilliseconds"
CALLEE_DEPTH = f"{PREFIX}.calleeDepth"
CALLEE_NATIVE = f"{PREFIX}.calleeNativeInline"
CALLEE_RED_ZONE = f"{PREFIX}.calleeRedZoneBytes"

_SPECS = {
    CALLEE_RED_ZONE: {"title": "Callee red-zone byte limit", "type": "number", "default": 128, "minValue": 0,
                      "description": "Maximum stack bytes below SP used by scratch proofs, capped by the platform ABI's red zone. Zero disables this; unknown and kernel platforms have none."},
    CALLEE_NATIVE: {"title": "Prefer native callee inlining", "type": "boolean", "default": False,
                    "description": "Compare native user-level inlining for register-only summaries. Requires Flow fixes for recorded undo; frame-dependent helpers use direct substitution."},
    CALLEE_DEPTH: {"title": "Callee summary depth", "type": "number", "default": 4, "minValue": 1,
                   "description": "Maximum nested summary depth. Recursive and tail-call helpers remain excluded."},
    CALLEES: {"title": "Pure-callee summaries", "type": "boolean", "default": True,
               "description": "Expose proven arithmetic helpers, including private register conventions, before MLIL generation."},
    CALLEE_INSTRUCTIONS: {"title": "Callee instruction limit", "type": "number", "default": 64, "minValue": 1,
                          "description": "Maximum lifted IL instructions in an arithmetic helper."},
    CALLEE_NODES: {"title": "Callee expression limit", "type": "number", "default": 2000, "minValue": 1,
                   "description": "Maximum expression nodes visited when constructing or checking a callee summary."},
    CALLEE_FRAME_BYTES: {"title": "Callee frame byte limit", "type": "number", "default": 256, "minValue": 1,
                         "description": "Maximum distinct stack bytes modeled by one callee or caller-frame proof."},
    CALLEE_CALLER_INSTRUCTIONS: {"title": "Callee caller instruction limit", "type": "number", "default": 4096,
                                 "minValue": 1, "description": "Maximum caller instructions copied or inspected to prove scratch writes dead."},
    CALLEE_SITES: {"title": "Callee call-site limit", "type": "number", "default": 64, "minValue": 1,
                   "description": "Maximum calls considered for summary substitution in one function analysis run."},
    CALLEE_PROOF_MS: {"title": "Callee proof timeout (milliseconds)", "type": "number", "default": 500,
                      "minValue": 1, "description": "Maximum solver time for one callee/frame proof, also bounded by the function allowance."},
    ENABLED: {"title": "Enabled", "type": "boolean", "default": True,
              "description": "Global kill switch. When off, D-810 never rewrites anything."},
    TIER2: {"title": "Generic MBA simplifier (Tier 2)", "type": "boolean", "default": True,
            "description": "Use CoBRA (if installed) and the built-in linear-MBA simplifier."},
    MAX_VARS: {"title": "Tier 2 variable limit", "type": "number", "default": 4, "minValue": 1, "maxValue": 6,
               "description": "CoBRA skips subexpressions with more distinct variables (the linear simplifier counts atoms instead, up to 8)."},
    MAX_NODES: {"title": "Expression node limit", "type": "number", "default": 200, "minValue": 10, "maxValue": 5000,
                "description": "Expressions larger than this are skipped and logged."},
    MAX_ITERATIONS: {"title": "Iteration cap", "type": "number", "default": 10, "minValue": 1, "maxValue": 100,
                     "description": "Maximum rewrite passes per function."},
    DISABLED_RULES: {"title": "Disabled rules", "type": "array", "elementType": "string", "default": [],
                     "description": "Tier 1 rule names to skip."},
    SKIP_FUNCTIONS: {"title": "Skipped functions", "type": "array", "elementType": "string", "default": [],
                     "description": "Function start addresses (hex) that D-810 never rewrites."},
    FUNCTION_SECONDS: {"title": "Time budget per function (seconds)", "type": "number", "default": 20,
                       "minValue": 1, "maxValue": 600,
                       "description": "Planning and solving time shared by the MLIL and HLIL passes of one "
                                      "analysis run. When it runs out, already verified rewrites are applied "
                                      "and nothing new is attempted. At most half of the "
                                      "analysis.limits.maxFunctionAnalysisTime a binary was opened with, which "
                                      "D-810 raises to twice this budget for a binary opened with D-810 enabled."},
    FLOW_FIXES: {"title": "Flow fixes", "type": "boolean", "default": True,
                 "description": "For functions D-810 is enabled on: resolve jumps and calls through tables of code "
                                "pointers that nothing writes, inline identity wrappers, and treat the read-only "
                                "ranges as constant. All auto analysis, undone when D-810 is turned off."},
    READ_ONLY_RANGES: {"title": "Read-only ranges", "type": "array", "elementType": "string", "default": [],
                       "description": "Address ranges whose data D-810 treats as constant, as 0xSTART-0xEND "
                                      "(end included). No checks: use it for data you know is never written."},
    UNFLATTEN: {"title": "Unflatten control flow", "type": "boolean", "default": True,
                "description": "For functions D-810 is enabled on: remove control-flow flattening by giving each "
                               "dispatcher entry its own path to the handler it reaches. Duplicates code where "
                               "needed and never changes behaviour."},
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


def callee_limits(ctx):
    from .callees.lift import Limits

    return Limits(instructions=ctx.get_setting_uint64(CALLEE_INSTRUCTIONS),
                  nodes=ctx.get_setting_uint64(CALLEE_NODES), frame_bytes=ctx.get_setting_uint64(CALLEE_FRAME_BYTES),
                  caller_instructions=ctx.get_setting_uint64(CALLEE_CALLER_INSTRUCTIONS),
                  sites=ctx.get_setting_uint64(CALLEE_SITES), proof_ms=ctx.get_setting_uint64(CALLEE_PROOF_MS),
                  depth=ctx.get_setting_uint64(CALLEE_DEPTH), red_zone_bytes=ctx.get_setting_uint64(CALLEE_RED_ZONE))


def skipped_functions(ctx) -> set[int]:
    out = set()
    for text in ctx.get_setting_string_list(SKIP_FUNCTIONS):
        try:
            out.add(int(text, 16))
        except ValueError:
            continue
    return out
