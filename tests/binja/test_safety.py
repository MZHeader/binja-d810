import random

import pytest
from binaryninja import MediumLevelILOperation, Settings
from helpers import function_named, lift_return, manifest, open_sample, set_types, signature, strip_versions

from binja_d810.binja import log, workflow
from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import Var, is_mba, substitute, variables
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import Verdict, check_equivalent

TYPES = {
    "safety_reassign": "uint32_t safety_reassign(uint32_t x, uint32_t y)",
    "safety_u8": "uint8_t safety_u8(uint8_t x, uint8_t y)",
    "safety_load": "uint32_t safety_load(uint32_t* p, uint32_t y)",
}
_LOADS = (MediumLevelILOperation.MLIL_LOAD, MediumLevelILOperation.MLIL_LOAD_STRUCT)


@pytest.fixture(scope="module", params=["O0", "O2"])
def safety(request):
    bv = open_sample(f"safety_{request.param}.dylib", d810=True)
    set_types(bv, TYPES)
    yield request.param, bv
    bv.file.close()


def _loads(func) -> int:
    count = 0
    for instr in func.mlil.instructions:
        stack = [instr]
        while stack:
            node = stack.pop()
            if node.operation in _LOADS:
                count += 1
            stack.extend(op for op in node.operands if hasattr(op, "operation"))
    return count


def test_safety_reassign_is_equivalent(safety):
    _, bv = safety
    lifted = strip_versions(lift_return(function_named(bv, "safety_reassign")))
    reference = parse("(x ^ y) + 2 * (x & y) + (3 * x + 1)", 32)
    assert check_equivalent(lifted, reference, use_z3=False) is not Verdict.REFUTED


def test_safety_u8_is_simplified_without_assuming_upper_bits(safety):
    # At -O2 Binary Ninja reads the 8-bit parameters both as bytes and as 32-bit register fields
    # (y.w1 & x.w0). The lifter relates them through the whole registers, whose upper bits stay
    # unknown: the check below draws them at random.
    _, bv = safety
    func = function_named(bv, "safety_u8")
    plain = strip_versions(lift_return(func))
    assert {v.name for v in variables(plain)} <= {"x", "y"}, plain
    assert not is_mba(plain), plain
    report = log.latest_report(func)
    assert report is not None and report.rewrites, "v1 left safety_u8 untouched at -O2"
    rng = random.Random(8)
    widths = {v.name: v.width for v in variables(plain)}
    for x in range(0, 256, 3):
        for y in range(256):
            env = {"x": x | (rng.getrandbits(widths.get("x", 8)) & ~0xFF), "y": y | (rng.getrandbits(widths.get("y", 8)) & ~0xFF)}
            assert evaluate(plain, env) & 0xFF == ((x ^ y) + 2 * (x & y)) & 0xFF, (x, y)


def test_safety_load_keeps_single_load(safety):
    level, bv = safety
    func = function_named(bv, "safety_load")
    plain = open_sample(f"safety_{level}.dylib", d810=False)
    try:
        set_types(plain, TYPES)
        assert _loads(func) <= _loads(function_named(plain, "safety_load"))
    finally:
        plain.file.close()
    lifted = strip_versions(lift_return(func), params=("y",))
    (load_leaf,) = [v for v in variables(lifted) if v.name != "y"]
    candidate = substitute(lifted, {load_leaf.name: Var("m", 32)})
    assert check_equivalent(candidate, parse("(m ^ y) + 2 * (m & y)", 32), use_z3=False) is not Verdict.REFUTED


def test_reanalysis_is_idempotent_and_toggle_restores():
    entries = [e for e in manifest() if e["category"] == "d810"][:3]
    bv = open_sample("mba_O2.dylib", d810=True)
    try:
        set_types(bv, {e["name"]: signature(e) for e in entries})
        funcs = [function_named(bv, e["name"]) for e in entries]
        before = ["\n".join(map(str, f.mlil.instructions)) for f in funcs]
        for f in funcs:
            f.reanalyze()
        bv.update_analysis_and_wait()
        funcs = [function_named(bv, e["name"]) for e in entries]
        assert ["\n".join(map(str, f.mlil.instructions)) for f in funcs] == before
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
        for f in funcs:
            f.reanalyze()
        bv.update_analysis_and_wait()
        for e in entries:
            assert is_mba(strip_versions(lift_return(function_named(bv, e["name"]))))
    finally:
        bv.file.close()
