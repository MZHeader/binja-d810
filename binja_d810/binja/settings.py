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
SCRATCH = f"{PREFIX}.scratchSlots"
SCRATCH_INSTRUCTIONS = f"{PREFIX}.scratchInstructions"
SCRATCH_NODES = f"{PREFIX}.scratchNodes"
SCRATCH_BYTES = f"{PREFIX}.scratchFrameBytes"
SCRATCH_VISITS = f"{PREFIX}.scratchLivenessVisits"
SCRATCH_ROUNDS = f"{PREFIX}.scratchRounds"
SCRATCH_DEPTH = f"{PREFIX}.scratchAddressDepth"
FLAGS = f"{PREFIX}.flagConditions"
FLAG_INSTRUCTIONS = f"{PREFIX}.flagInstructions"
FLAG_NODES = f"{PREFIX}.flagNodes"
FLAG_WORK = f"{PREFIX}.flagWork"
FLAG_CANDIDATES = f"{PREFIX}.flagCandidates"
FLAG_CONDITIONS = f"{PREFIX}.flagConditionsPerPass"
FLAG_MERGES = f"{PREFIX}.flagMergeDepth"
FLAG_PROOF_MS = f"{PREFIX}.flagProofMilliseconds"
ALLOCATIONS = f"{PREFIX}.allocationRoundTrips"
ALLOCATION_INSTRUCTIONS = f"{PREFIX}.allocationInstructions"
ALLOCATION_NODES = f"{PREFIX}.allocationNodes"
ALLOCATION_SITES = f"{PREFIX}.allocationSites"
ALLOCATION_BYTES = f"{PREFIX}.allocationBytes"
ALLOCATION_WRAPPER = f"{PREFIX}.allocationWrapperInstructions"
ALLOCATION_DEPTH = f"{PREFIX}.allocationWrapperDepth"
ALLOCATION_EXPRESSION = f"{PREFIX}.allocationExpressionNodes"
ALLOCATION_CONTRACT = f"{PREFIX}.allocationContractBytes"

_SPECS = {
    ALLOCATIONS: {"title": "Simplify private allocation round trips", "type": "boolean", "default": True,
                  "description": "Requires explicit semantic allocation contracts. Preserve calls unless failure and effects are proved removable."},
    ALLOCATION_INSTRUCTIONS: {"title": "Allocation instruction limit", "type": "number", "default": 20000, "minValue": 1,
                              "description": "Maximum MLIL instructions considered by allocation proofs."},
    ALLOCATION_NODES: {"title": "Allocation proof work limit", "type": "number", "default": 40000, "minValue": 1,
                       "description": "Maximum IL/use nodes visited by allocation proofs per pass."},
    ALLOCATION_SITES: {"title": "Allocation call-site limit", "type": "number", "default": 64, "minValue": 1,
                       "description": "Maximum root call sites considered for allocation contracts per pass; wrapper and use traversal also obey the proof work limit."},
    ALLOCATION_BYTES: {"title": "Allocation byte limit", "type": "number", "default": 4096, "minValue": 1,
                       "description": "Maximum size of one modeled private object."},
    ALLOCATION_WRAPPER: {"title": "Allocation wrapper instruction limit", "type": "number", "default": 64, "minValue": 1,
                         "description": "Maximum instructions in a constant-argument or forwarding wrapper."},
    ALLOCATION_DEPTH: {"title": "Allocation wrapper depth", "type": "number", "default": 4, "minValue": 1,
                       "description": "Maximum nested allocation/deallocation wrappers."},
    ALLOCATION_EXPRESSION: {"title": "Allocation expression limit", "type": "number", "default": 2000, "minValue": 1,
                            "description": "Maximum nodes when lifting stored values and wrapper arguments."},
    ALLOCATION_CONTRACT: {"title": "Allocation contract byte limit", "type": "number", "default": 4096, "minValue": 1,
                          "description": "Maximum UTF-8 bytes in one explicit semantic contract."},
    FLAGS: {"title": "Recombine flag conditions", "type": "boolean", "default": True,
            "description": "Prove simpler comparisons for boolean flags and bounded conditional-compare merges."},
    FLAG_INSTRUCTIONS: {"title": "Flag instruction limit", "type": "number", "default": 20000, "minValue": 1,
                        "description": "Maximum IL instructions in a function considered for flag reconstruction."},
    FLAG_NODES: {"title": "Flag expression limit", "type": "number", "default": 2000, "minValue": 1,
                 "description": "Maximum nodes in a lifted flag expression."},
    FLAG_WORK: {"title": "Flag proof work limit", "type": "number", "default": 80000, "minValue": 1,
                "description": "Maximum IL, SSA-use and reconstruction visits per flag pass."},
    FLAG_CANDIDATES: {"title": "Flag comparison candidates", "type": "number", "default": 128, "minValue": 1,
                      "description": "Maximum comparison proof attempts per flag expression."},
    FLAG_CONDITIONS: {"title": "Flag conditions per pass", "type": "number", "default": 128, "minValue": 1,
                      "description": "Maximum condition roots inspected per MLIL or HLIL pass."},
    FLAG_MERGES: {"title": "Flag merge depth", "type": "number", "default": 8, "minValue": 1, "maxValue": 32,
                  "description": "Maximum nested conditional-compare merges followed through SSA."},
    FLAG_PROOF_MS: {"title": "Flag proof timeout (milliseconds)", "type": "number", "default": 200, "minValue": 1,
                    "description": "Maximum time per comparison proof, also bounded by the shared function deadline."},
    SCRATCH_DEPTH: {"title": "Scratch address depth", "type": "number", "default": 64, "minValue": 1,
                    "maxValue": 256, "description": "Maximum nested SSA definitions inspected to resolve a stack address."},
    SCRATCH: {"title": "Clean up private scratch slots", "type": "boolean", "default": True,
               "description": "Remove proven dead local stack writes and promote non-escaping stack accesses. Regenerated when disabled."},
    SCRATCH_INSTRUCTIONS: {"title": "Scratch instruction limit", "type": "number", "default": 20000, "minValue": 1,
                           "description": "Maximum MLIL instructions inspected or copied by one scratch pass."},
    SCRATCH_NODES: {"title": "Scratch proof node limit", "type": "number", "default": 40000, "minValue": 1,
                    "description": "Maximum expression, use and control-flow nodes visited in one scratch cleanup round."},
    SCRATCH_BYTES: {"title": "Scratch frame byte limit", "type": "number", "default": 4096, "minValue": 1,
                    "description": "Maximum distinct local stack bytes tracked by scratch liveness."},
    SCRATCH_VISITS: {"title": "Scratch liveness work limit", "type": "number", "default": 100000, "minValue": 1,
                     "description": "Maximum instruction visits while solving the byte-liveness fixpoint."},
    SCRATCH_ROUNDS: {"title": "Scratch cleanup rounds", "type": "number", "default": 4, "minValue": 1,
                     "description": "Maximum cleanup rounds per pass, before MBA rewriting and after control-flow recovery."},
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


def scratch_limits(ctx):
    from .scratch import Limits

    return Limits(instructions=ctx.get_setting_uint64(SCRATCH_INSTRUCTIONS), nodes=ctx.get_setting_uint64(SCRATCH_NODES),
                  frame_bytes=ctx.get_setting_uint64(SCRATCH_BYTES), visits=ctx.get_setting_uint64(SCRATCH_VISITS),
                  rounds=ctx.get_setting_uint64(SCRATCH_ROUNDS), address_depth=ctx.get_setting_uint64(SCRATCH_DEPTH))


def flag_limits(ctx):
    from ..flags.recombine import Limits
    return Limits(instructions=ctx.get_setting_uint64(FLAG_INSTRUCTIONS), nodes=ctx.get_setting_uint64(FLAG_NODES),
                  work=ctx.get_setting_uint64(FLAG_WORK), candidates=ctx.get_setting_uint64(FLAG_CANDIDATES),
                  conditions=ctx.get_setting_uint64(FLAG_CONDITIONS), merges=ctx.get_setting_uint64(FLAG_MERGES),
                  proof_ms=ctx.get_setting_uint64(FLAG_PROOF_MS))


def allocation_limits(ctx):
    from .allocation_roundtrips import Limits
    return Limits(instructions=ctx.get_setting_uint64(ALLOCATION_INSTRUCTIONS), nodes=ctx.get_setting_uint64(ALLOCATION_NODES),
                  sites=ctx.get_setting_uint64(ALLOCATION_SITES), bytes=ctx.get_setting_uint64(ALLOCATION_BYTES),
                  wrapper_instructions=ctx.get_setting_uint64(ALLOCATION_WRAPPER), depth=ctx.get_setting_uint64(ALLOCATION_DEPTH),
                  expression_nodes=ctx.get_setting_uint64(ALLOCATION_EXPRESSION), contract_bytes=ctx.get_setting_uint64(ALLOCATION_CONTRACT))


def skipped_functions(ctx) -> set[int]:
    out = set()
    for text in ctx.get_setting_string_list(SKIP_FUNCTIONS):
        try:
            out.add(int(text, 16))
        except ValueError:
            continue
    return out
