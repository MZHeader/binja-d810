import random

import pytest
from binaryninja import MediumLevelILOperation, Settings
import oracle
from arch import IDS, MATRIX, expect_miss, sample
from helpers import function_named, lift_return, manifest, open_sample, set_types, signature, strip_versions

from binja_d810.binja import log, workflow
from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import UnOp, Var, is_mba, substitute, variables
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import Verdict, check_equivalent

TYPES = {
    "safety_reassign": "uint32_t safety_reassign(uint32_t x, uint32_t y)",
    "safety_u8": "uint8_t safety_u8(uint8_t x, uint8_t y)",
    "safety_load": "uint32_t safety_load(uint32_t* p, uint32_t y)",
}
_LOADS = (MediumLevelILOperation.MLIL_LOAD, MediumLevelILOperation.MLIL_LOAD_STRUCT)


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def safety(request):
    build, level = request.param
    bv = open_sample(sample("safety", build, level), d810=True)
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


def _at_width(e, width):
    return e if e.width == width else UnOp("trunc", e, width) if e.width > width else UnOp("zx", e, width)


def test_safety_reassign_is_equivalent(safety):
    _, bv = safety
    lifted = _at_width(strip_versions(lift_return(function_named(bv, "safety_reassign"))), 32)
    reference = parse("(x ^ y) + 2 * (x & y) + (3 * x + 1)", 32)
    assert check_equivalent(lifted, reference, use_z3=False) is not Verdict.REFUTED


def test_safety_u8_is_simplified_without_assuming_upper_bits(safety, request):
    # x86 and ARM64 -O2 read the 8-bit parameters through wider registers or fields (y.w1 & x.w0,
    # var_8:5.b). The final HLIL must compute (x + y) & 0xff for every value of the registers'
    # upper bits: the oracle models them as unknown and checks all 2**16 low-byte pairs.
    (build, level), bv = safety
    expect_miss(request, "safety_u8", f"{build}-{level}")
    func = function_named(bv, "safety_u8")
    final = oracle.function_result(func)
    assert oracle.compare(final, oracle.reference("(x ^ y) + 2 * (x & y)", 8), 8).kind in ("exhaustive", "proven")
    # The returned computation is simplified. Dead byte stores into a shared stack slot may remain at
    # -O0 (liveness is per variable version, not per byte): presentation only, listed in the report.
    (ret,) = [str(i) for i in func.hlil.instructions if i.operation.name == "HLIL_RET"]
    assert "^" not in ret and "&" not in ret, ret
    # The MLIL lift agrees, with random upper register bits.
    plain = strip_versions(lift_return(func))
    if {v.name for v in variables(plain)} <= {"x", "y"}:
        rng = random.Random(8)
        widths = {v.name: v.width for v in variables(plain)}
        for x in range(0, 256, 3):
            for y in range(256):
                env = {"x": x | (rng.getrandbits(widths.get("x", 8)) & ~0xFF),
                       "y": y | (rng.getrandbits(widths.get("y", 8)) & ~0xFF)}
                assert evaluate(plain, env) & 0xFF == ((x ^ y) + 2 * (x & y)) & 0xFF, (x, y)


def test_safety_load_keeps_single_load(safety):
    (build, level), bv = safety
    func = function_named(bv, "safety_load")
    plain = open_sample(sample("safety", build, level), d810=False)
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
