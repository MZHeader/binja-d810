"""Shift semantics against Binary Ninja's IL and the hardware (spec section 6.5).

tests/samples/src/shifts_*.S pairs each variable-count function (`shl8_var`) with constant-input
functions whose result Binary Ninja's dataflow folds (`shl8_by8`). Three references must agree:
the IR evaluator applied to the lifted variable-count function, Binary Ninja's folded constant,
and the architecture manual's definition (written out here).
"""

import pytest
from binaryninja import MediumLevelILOperation
from helpers import open_sample

from binja_d810.binja.lift import lift
from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import mask, to_signed, variables


def hardware(arch: str, op: str, x: int, count: int, width: int) -> int:
    """Intel SDM SHL/SHR/SAR: count masked to 5 bits (6 for 64-bit operands), any operand size.
    ARM ARM LSLV/LSRV/ASRV: count modulo the register width (32 or 64)."""
    if arch == "x86":
        c = count & (0x3F if width == 64 else 0x1F)
    else:
        c = count % width
    m = mask(width)
    if op == "shl":
        return (x << c) & m
    if op == "lshr":
        return (x & m) >> c
    return (to_signed(x, width) >> c) & m


# (file, variable-count function, constant function, op, x, count, width)
CASES = [
    ("shifts_x86_64.so", "shl8_var", "shl8_by8", "shl", 1, 8, 8),
    ("shifts_x86_64.so", "sar8_var", "sar8_by8", "ashr", 0x80, 8, 8),
    ("shifts_x86_64.so", "shr8_var", "shr8_imm9", "lshr", 0x80, 9, 8),
    ("shifts_x86_64.so", "shl32_var", "shl32_by33", "shl", 1, 33, 32),
    ("shifts_x86_64.so", "shl64_var", "shl64_by65", "shl", 1, 65, 64),
    ("shifts_arm64.dylib", "lsl32_var", "lsl32_by33", "shl", 1, 33, 32),
    ("shifts_arm64.dylib", "lsl64_var", "lsl64_by65", "shl", 1, 65, 64),
    ("shifts_arm64.dylib", "asr32_var", "asr32_by32", "ashr", 0x80000000, 32, 32),
    ("shifts_arm64.dylib", "lsr32_var", "lsr32_by32", "lshr", 0x80000000, 32, 32),
]
# Constant-only fixtures: Binary Ninja's folded value against the manual.
FOLDED = [
    ("shifts_x86_64.so", "shr16_by16", "lshr", 0x8000, 16, 16),
    ("shifts_x86_64.so", "shl8_imm9", "shl", 1, 9, 8),
    ("shifts_x86_64.so", "shl16_imm17", "shl", 1, 17, 16),
    ("shifts_x86_64.so", "shl32_imm33", "shl", 1, 33, 32),
    ("shifts_x86_64.so", "sar8_imm9", "ashr", 0x80, 9, 8),
    ("shifts_arm64.dylib", "lsr64_by65", "lshr", 1 << 63, 65, 64),
]


@pytest.fixture(scope="module")
def views():
    opened = {name: open_sample(name, d810=False) for name in ("shifts_x86_64.so", "shifts_arm64.dylib")}
    yield opened
    for bv in opened.values():
        bv.file.close()


def _function(bv, name):
    for candidate in (name, "_" + name):
        found = bv.get_functions_by_name(candidate)
        if found:
            return found[0]
    raise LookupError(name)


def _returned_constant(func) -> int:
    (ret,) = [i for i in func.mlil.instructions if i.operation == MediumLevelILOperation.MLIL_RET]
    value = ret.src[0].value
    assert value.type.name in ("ConstantValue", "ConstantPointerValue"), value
    return value.value


def _arch(file_name: str) -> str:
    return "x86" if "x86" in file_name else "arm64"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[1])
def test_lifted_shift_matches_binary_ninja_and_hardware(views, case):
    file_name, var_func, const_func, op, x, count, width = case
    bv = views[file_name]
    expected = hardware(_arch(file_name), op, x, count, width)
    assert _returned_constant(_function(bv, const_func)) & mask(width) == expected

    func = _function(bv, var_func)
    (ret,) = [i for i in func.mlil.instructions if i.operation == MediumLevelILOperation.MLIL_RET]
    ir = lift(ret.src[0].ssa_form, root_instr=ret, check_versions=False)
    assert ir is not None, str(ret)
    env = {}
    for leaf in variables(ir):
        name = leaf.ref.var.name if hasattr(leaf.ref, "var") else leaf.name
        env[leaf.name] = x if name.startswith("arg1") else count if name.startswith("arg2") else 0
    assert evaluate(ir, env) & mask(width) == expected, (str(ret), ir)


@pytest.mark.parametrize("case", FOLDED, ids=lambda c: c[1])
def test_binary_ninja_folds_like_the_hardware(views, case):
    file_name, const_func, op, x, count, width = case
    folded = _returned_constant(_function(views[file_name], const_func)) & mask(width)
    assert folded == hardware(_arch(file_name), op, x, count, width)
