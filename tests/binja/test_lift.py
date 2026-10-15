import pytest
from binaryninja import MediumLevelILOperation, SSAVariable
from helpers import (
    function_named, lift_return, manifest, open_sample, return_expr, set_types, signature, strip_versions,
)

from binja_d810.binja.lift import SUPPORTED_OPS, Opaque, candidate_roots, lift
from binja_d810.ir.expr import is_mba, variables
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import Verdict, check_equivalent

SAFETY_TYPES = {
    "safety_reassign": "uint32_t safety_reassign(uint32_t x, uint32_t y)",
    "safety_u8": "uint8_t safety_u8(uint8_t x, uint8_t y)",
    "safety_load": "uint32_t safety_load(uint32_t* p, uint32_t y)",
}


@pytest.fixture(scope="module", params=["O0", "O2"])
def plain_mba(request):
    bv = open_sample(f"mba_{request.param}.dylib", d810=False)
    entries = [e for e in manifest() if e["category"] == "d810"][:10]
    set_types(bv, {e["name"]: signature(e) for e in entries})
    yield bv, entries
    bv.file.close()


@pytest.fixture(scope="module", params=["O0", "O2"])
def plain_safety(request):
    bv = open_sample(f"safety_{request.param}.dylib", d810=False)
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


def test_lift_reconstructs_mba_split_across_instructions(plain_mba):
    bv, entries = plain_mba
    for entry in entries:
        func = function_named(bv, entry["name"])
        instr, expr = _result_definition(func)
        lifted = lift(expr.ssa_form, root_instr=instr, max_nodes=10_000)
        assert lifted is not None and is_mba(lifted), entry["name"]
        plain = strip_versions(lifted)
        assert {v.name for v in variables(plain)} <= {"x", "y", "z"}, entry["name"]
        obf = parse(entry["obfuscated"], entry["width"])
        assert check_equivalent(plain, obf, use_z3=False) is not Verdict.REFUTED, entry["name"]


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
    lifted = strip_versions(lift_return(func))
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
