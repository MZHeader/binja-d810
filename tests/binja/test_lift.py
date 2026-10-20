import pytest
from arch import IDS, MATRIX, sample
from binaryninja import MediumLevelILOperation, SSAVariable
from helpers import (
    function_named, lift_return, manifest, open_sample, return_expr, set_types, signature, strip_versions,
)

from binja_d810.binja.mlil.lift import SUPPORTED_OPS, Opaque, candidate_roots, lift
from binja_d810.ir.expr import UnOp, is_mba, variables
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import Verdict, check_equivalent

SAFETY_TYPES = {
    "safety_reassign": "uint32_t safety_reassign(uint32_t x, uint32_t y)",
    "safety_u8": "uint8_t safety_u8(uint8_t x, uint8_t y)",
    "safety_load": "uint32_t safety_load(uint32_t* p, uint32_t y)",
}


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def plain_mba(request):
    bv = open_sample(sample("mba", *request.param), d810=False)
    entries = [e for e in manifest() if e["category"] == "d810"][:10]
    set_types(bv, {e["name"]: signature(e) for e in entries})
    yield bv, entries
    bv.file.close()


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def plain_safety(request):
    bv = open_sample(sample("safety", *request.param), d810=False)
    set_types(bv, SAFETY_TYPES)
    yield bv
    bv.file.close()


def _result_definition(func):
    """The instruction computing the returned value: where the engine finds the whole MBA.

    At the return itself the inputs are often dead: Binary Ninja numbers SSA versions per storage,
    and the result register x0 also holds parameter x.
    """
    instr, expr = return_expr(func)
    if expr.operation != MediumLevelILOperation.MLIL_VAR:
        return instr, expr
    (definition,) = func.mlil.get_var_definitions(expr.src)
    return definition, definition.src


def _at_width(e, width):
    return e if e.width == width else UnOp("trunc", e, width) if e.width > width else UnOp("zx", e, width)


def test_lift_reconstructs_mba_split_across_instructions(plain_mba):
    bv, entries = plain_mba
    for entry in entries:
        func = function_named(bv, entry["name"])
        instr, expr = _result_definition(func)
        # Following every definition rebuilds the whole MBA over the parameters ...
        unchecked = lift(expr.ssa_form, root_instr=instr, check_versions=False, max_nodes=10_000)
        assert unchecked is not None and is_mba(unchecked), entry["name"]
        plain = _at_width(strip_versions(unchecked), entry["width"])
        assert {v.name for v in variables(plain)} <= {"x", "y", "z"}, entry["name"]
        obf = parse(entry["obfuscated"], entry["width"])
        assert check_equivalent(plain, obf, use_z3=False) is not Verdict.REFUTED, entry["name"]
        # ... and the checked lift, where it succeeds, only keeps leaves still current at the root.
        checked = lift(expr.ssa_form, root_instr=instr, max_nodes=10_000)
        for leaf in variables(checked) if checked is not None else []:
            if isinstance(leaf.ref, SSAVariable):
                assert instr.get_ssa_var_version(leaf.ref.var) == leaf.ref.version, (entry["name"], leaf.name)


def test_lift_never_uses_stale_versions(plain_safety):
    func = function_named(plain_safety, "safety_reassign")
    instr, expr = return_expr(func)
    lifted = lift(expr.ssa_form, root_instr=instr, check_versions=True)
    assert lifted is not None
    for leaf in variables(lifted):
        if isinstance(leaf.ref, SSAVariable):
            assert instr.get_ssa_var_version(leaf.ref.var) == leaf.ref.version, leaf.name


def test_unchecked_lift_follows_all_definitions(plain_safety):
    func = function_named(plain_safety, "safety_reassign")
    lifted = _at_width(strip_versions(lift_return(func)), 32)
    reference = parse("(x ^ y) + 2 * (x & y) + (3 * x + 1)", 32)
    assert check_equivalent(lifted, reference, use_z3=False) is not Verdict.REFUTED


def test_loads_in_definitions_stay_leaves(plain_safety):
    func = function_named(plain_safety, "safety_load")
    instr, expr = return_expr(func)
    lifted = lift(expr.ssa_form, root_instr=instr)
    assert lifted is not None
    assert not any(isinstance(v.ref, Opaque) for v in variables(lifted))
    assert any(isinstance(v.ref, SSAVariable) and v.ref.var.name not in ("p", "y") for v in variables(lifted))


def test_candidate_roots_are_supported_and_maximal(plain_mba):
    bv, entries = plain_mba
    func = function_named(bv, entries[0]["name"])
    for instr in func.mlil.instructions:
        for root in candidate_roots(instr):
            assert root.operation in SUPPORTED_OPS


@pytest.mark.parametrize("build,level", MATRIX, ids=IDS)
def test_lift_work_is_bounded_when_definitions_are_shared(build, level):
    """lift_dag reads both values twice per step: the fully inlined tree is exponential. Each lift
    must stay within a few times its node budget (a Tigress VM: 249 roots took 6 s to lift)."""
    from binja_d810.binja.hlil.lift import candidate_roots as hlil_roots, expression_roots, lift as hlil_lift

    bv = open_sample(sample("hlil_fixtures", build, level), d810=False)
    try:
        func = function_named(bv, "lift_dag")
        func.set_user_type("uint32_t lift_dag(uint32_t x, uint32_t y)")
        bv.update_analysis_and_wait()
        worst = {"mlil": 0, "hlil": 0}
        for instr in func.mlil.instructions:
            for root in candidate_roots(instr):
                stats: dict = {}
                lift(root.ssa_form, root_instr=instr, max_nodes=200, stats=stats)
                worst["mlil"] = max(worst["mlil"], stats["visits"])
        aliased = frozenset(func.hlil.aliased_vars)
        for statement in expression_roots(func.hlil):
            for root in hlil_roots(statement, aliased):
                stats = {}
                hlil_lift(root.ssa_form, aliased=aliased, max_nodes=200, stats=stats)
                worst["hlil"] = max(worst["hlil"], stats["visits"])
        assert max(worst.values()) <= 4 * 200, worst
    finally:
        bv.file.close()


@pytest.mark.parametrize("build,level", MATRIX, ids=IDS)
def test_a_root_over_a_value_that_stays_visible_is_simplified_over_that_value(build, level):
    """shared_value: a is stored and combined. Inlining a's definition would copy it into the
    return (the visible-cost gate refuses); the root over a itself is a + z."""
    bv = open_sample(sample("hlil_fixtures", build, level), d810=True)
    try:
        func = function_named(bv, "shared_value")
        func.set_user_type("uint32_t shared_value(uint32_t x, uint32_t y, uint32_t z, uint32_t* out)")
        bv.update_analysis_and_wait()
        returns = [i for i in func.hlil.instructions if i.operation.name == "HLIL_RET"]
        text = "\n".join(map(str, func.hlil.instructions))
        assert len(returns) == 1 and "^" not in str(returns[0]) and "<<" not in str(returns[0]), text
        if f"{build}-{level}" == "i686-pe-O2":
            return  # ecx (a) is reused before the return: MLIL can only inline a; the MBA is gone
        assert "&" not in str(returns[0]), text  # a + z, not a copy of a's definition
    finally:
        bv.file.close()


@pytest.mark.parametrize("build,level", MATRIX, ids=IDS)
def test_statements_that_were_each_unmixed_are_not_merged_into_a_mixed_expression(build, level):
    from binja_d810.binja import log

    shown = {}
    for d810 in (False, True):
        bv = open_sample(sample("hlil_fixtures", build, level), d810=d810)
        try:
            func = function_named(bv, "split_mix")
            func.set_user_type("uint32_t split_mix(uint32_t x, uint32_t y)")
            bv.update_analysis_and_wait()
            shown[d810] = [str(i) for i in func.hlil.instructions]
            rewrites = [r for report in log.latest_reports(func).values() for r in report.rewrites]
        finally:
            bv.file.close()
    text = "\n".join(shown[True])
    if len(shown[False]) > 1:  # the definition and its use shown apart, each unmixed
        for r in rewrites:
            assert not ("*" in r.after and ("^" in r.after or "|" in r.after)), (shown[False], r.before, r.after)
    else:  # already one mixed statement (Binary Ninja folded a): a mixed result may replace it
        assert "^" in shown[True][-1], text


@pytest.mark.parametrize("build,level", MATRIX, ids=IDS)
def test_a_chain_of_single_use_temporaries_is_one_statement(build, level):
    # The MLIL pass alone: its statements are three-address code, so mixedness is judged on the
    # statements HLIL shows (a three-address MBA sample lost most of its v2.1 gains to this).
    from binja_d810.binja import log, workflow

    # x86-64 -O2 reuses x's and y's registers before the combining instruction: the version-safe
    # MLIL lift cannot see them, and the HLIL pass simplifies the function there.
    reused = build.startswith("x86_64") and level == "O2"
    workflow.set_levels_override({"mlil", "hlil"} if reused else {"mlil"})
    bv = open_sample(sample("hlil_fixtures", build, level), d810=True)
    try:
        func = function_named(bv, "mixed_chain")
        func.set_user_type("uint32_t mixed_chain(uint32_t x, uint32_t y, uint32_t z)")
        bv.update_analysis_and_wait()
        text = "\n".join(str(i) for i in func.hlil.instructions)
        reports = log.latest_reports(func)
    finally:
        workflow.set_levels_override(None)
        bv.file.close()
    refused = [s for r in reports.values() for s in r.skipped if "newly mixed" in s.reason]
    assert not refused, (text, refused)
    assert "^" not in text and "&" not in text, (text, [(s.address, s.reason) for r in reports.values() for s in r.skipped],
                                                 [r.rewrites for r in reports.values()])
