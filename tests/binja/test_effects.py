"""Effect policy in Binary Ninja: loads, calls, aliased and volatile reads
keep their count and order; values already materialized from them remain usable leaves."""

import pytest
from arch import IDS, MATRIX, expect_miss, sample
from helpers import function_named, lift_return, open_sample, set_types, strip_versions

from binja_d810.binja import log, settings
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


def _hlil_simplified(func) -> bool:
    """The final HLIL no longer mixes ^ or & with arithmetic: (m ^ y) + 2 * (m & y) became m + y."""
    text = "\n".join(map(str, func.hlil.instructions))
    return "^" not in text and " & " not in text


def _count_hlil_derefs(func) -> int:
    return _count(func.hlil, {"HLIL_DEREF", "HLIL_DEREF_FIELD", "HLIL_ARRAY_INDEX"})


def _pair(file_name, types):
    # This suite tests the expression passes' effect boundary. The optional earlier summary
    # pass can now prove eff_source arithmetic-only and eff_sink empty from their bodies.
    # Its separate fixtures test those removals and keep actual writes/unknown calls.
    on = open_sample(file_name, d810=True, options={settings.CALLEES: False})
    off = open_sample(file_name, d810=False)
    set_types(on, types)
    set_types(off, types)
    return on, off


@pytest.fixture(scope="module")
def asm():
    on, off = _pair("effects_x86_64.so", ASM_TYPES)
    yield on, off
    on.file.close()
    off.file.close()


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def c_code(request):
    on, off = _pair(sample("effects", *request.param), C_TYPES)
    yield on, off, "-".join(request.param)
    on.file.close()
    off.file.close()


def test_load_inside_a_root_is_never_deleted(asm):
    on, off = asm
    func = function_named(on, "eff_load_and_zero")
    assert _count(func.mlil, LOADS) == _count(function_named(off, "eff_load_and_zero").mlil, LOADS) == 1
    report = log.latest_report(func)
    assert report is not None and any(r.reason.startswith("effect:") for r in report.skipped), report


def test_pure_part_of_an_effectful_root_is_simplified(asm):
    on, off = asm
    func = function_named(on, "eff_load_add")
    assert _count(func.mlil, LOADS) == _count(function_named(off, "eff_load_add").mlil, LOADS) == 1
    hlil = "\n".join(map(str, func.hlil.instructions))
    assert "^" not in hlil and "&" not in hlil, hlil


def test_call_result_is_a_stable_leaf(c_code, request):
    on, off, build_level = c_code
    expect_miss(request, "eff_call_result", build_level)
    func = function_named(on, "eff_call_result")
    assert _count(func.mlil, CALLS) == _count(function_named(off, "eff_call_result").mlil, CALLS) == 1
    assert _hlil_simplified(func)


def test_aliased_value_is_kept_and_simplified(c_code, request):
    on, off, build_level = c_code
    expect_miss(request, "eff_aliased", build_level)
    func = function_named(on, "eff_aliased")
    plain = function_named(off, "eff_aliased")
    assert _count(func.mlil.ssa_form, {"MLIL_VAR_ALIASED"}) == _count(plain.mlil.ssa_form, {"MLIL_VAR_ALIASED"})
    assert _count(func.mlil, LOADS) == _count(plain.mlil, LOADS)
    assert _hlil_simplified(func)


def test_volatile_reads_are_not_merged(c_code):
    # (g ^ y) + 2 * (g & y) with g volatile is two reads; x + y over one read would be wrong.
    on, off, _ = c_code
    func = function_named(on, "eff_volatile")
    assert _count(func.mlil, LOADS) == _count(function_named(off, "eff_volatile").mlil, LOADS) == 2
    assert _count_hlil_derefs(func) == _count_hlil_derefs(function_named(off, "eff_volatile")) == 2
