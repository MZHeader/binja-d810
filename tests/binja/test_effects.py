"""Effect policy in Binary Ninja (spec sections 5.2 and 10): loads, calls, aliased and volatile reads
keep their count and order; values already materialized from them remain usable leaves."""

import pytest
from helpers import function_named, lift_return, open_sample, set_types, strip_versions

from binja_d810.binja import log
from binja_d810.ir.expr import Var, is_mba, substitute, variables
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import Verdict, check_equivalent

ASM_TYPES = {
    "eff_load_and_zero": "uint32_t eff_load_and_zero(uint32_t x, uint32_t y, uint32_t* p)",
    "eff_load_add": "uint32_t eff_load_add(uint32_t x, uint32_t y, uint32_t* p)",
}
C_TYPES = {
    "eff_call_result": "uint32_t eff_call_result(uint32_t x, uint32_t y)",
    "eff_aliased": "uint32_t eff_aliased(uint32_t x, uint32_t y)",
    "eff_volatile": "uint32_t eff_volatile(uint32_t y)",
}
LOADS = {"MLIL_LOAD", "MLIL_LOAD_STRUCT"}
CALLS = {"MLIL_CALL", "MLIL_CALL_UNTYPED", "MLIL_TAILCALL", "MLIL_TAILCALL_UNTYPED"}


def _count(il, names) -> int:
    count = 0
    for instr in il.instructions:
        stack = [instr]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not hasattr(node, "operation"):
                continue
            count += node.operation.name in names
            stack.extend(node.operands)
    return count


def _pair(file_name, types):
    on, off = open_sample(file_name, d810=True), open_sample(file_name, d810=False)
    set_types(on, types)
    set_types(off, types)
    return on, off


@pytest.fixture(scope="module")
def asm():
    on, off = _pair("effects_x86_64.so", ASM_TYPES)
    yield on, off
    on.file.close()
    off.file.close()


@pytest.fixture(scope="module", params=["O0", "O2"])
def c_code(request):
    on, off = _pair(f"effects_{request.param}.dylib", C_TYPES)
    yield on, off
    on.file.close()
    off.file.close()


def test_load_inside_a_root_is_never_deleted(asm):
    on, off = asm
    func = function_named(on, "eff_load_and_zero")
    assert _count(func.mlil, LOADS) == _count(function_named(off, "eff_load_and_zero").mlil, LOADS) == 1
    report = log.latest_report(func)
    assert report is not None and any(reason.startswith("effect:") for _, reason in report.skipped), report


def test_pure_part_of_an_effectful_root_is_simplified(asm):
    on, off = asm
    func = function_named(on, "eff_load_add")
    assert _count(func.mlil, LOADS) == _count(function_named(off, "eff_load_add").mlil, LOADS) == 1
    hlil = "\n".join(map(str, func.hlil.instructions))
    assert "^" not in hlil and "&" not in hlil, hlil


def test_call_result_is_a_stable_leaf(c_code):
    on, off = c_code
    func = function_named(on, "eff_call_result")
    assert _count(func.mlil, CALLS) == _count(function_named(off, "eff_call_result").mlil, CALLS) == 1
    assert not is_mba(strip_versions(lift_return(func)))


def test_aliased_value_is_kept_and_simplified(c_code):
    on, off = c_code
    func = function_named(on, "eff_aliased")
    plain = function_named(off, "eff_aliased")
    assert _count(func.mlil.ssa_form, {"MLIL_VAR_ALIASED"}) == _count(plain.mlil.ssa_form, {"MLIL_VAR_ALIASED"})
    assert _count(func.mlil, LOADS) == _count(plain.mlil, LOADS)
    assert not is_mba(strip_versions(lift_return(func)))


def test_volatile_reads_are_not_merged(c_code):
    on, off = c_code
    func = function_named(on, "eff_volatile")
    assert _count(func.mlil, LOADS) == _count(function_named(off, "eff_volatile").mlil, LOADS) == 2
    lifted = strip_versions(lift_return(func), params=("y",))
    loads = [v for v in variables(lifted) if v.name != "y"]
    assert len(loads) == 2, lifted
    named = substitute(lifted, {loads[0].name: Var("a", 32), loads[1].name: Var("b", 32)})
    first, second = parse("(a ^ y) + 2 * (b & y)", 32), parse("(b ^ y) + 2 * (a & y)", 32)
    assert Verdict.REFUTED in (check_equivalent(named, first, use_z3=False), check_equivalent(named, second, use_z3=False)) \
        and (check_equivalent(named, first, use_z3=False) is not Verdict.REFUTED
             or check_equivalent(named, second, use_z3=False) is not Verdict.REFUTED), named
