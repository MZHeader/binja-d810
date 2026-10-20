"""Shared helpers for tests that run inside Binary Ninja."""

import json
from pathlib import Path

import binaryninja
import pytest
from binaryninja import HighLevelILOperation, MediumLevelILOperation, RegisterValueType, SSAVariable

from binja_d810.binja import workflow
from binja_d810.binja.mlil.lift import Backing, lift
from binja_d810.ir.expr import Var, substitute, variables

BUILD = Path(__file__).resolve().parents[1] / "samples" / "build"
_CONSTANTS = (RegisterValueType.ConstantValue, RegisterValueType.ConstantPointerValue)


def manifest() -> list[dict]:
    return json.loads((BUILD / "manifest.json").read_text())


def open_sample(file_name: str, *, d810: bool, options: dict | None = None):
    path = BUILD / file_name
    if not path.exists():
        pytest.fail(f"{path} is missing; run tests/samples/build.sh")
    return binaryninja.load(str(path), options={workflow.ACTIVITY_SETTING: d810, **(options or {})})


def function_named(bv, name: str):
    for candidate in (name, "_" + name):
        funcs = bv.get_functions_by_name(candidate)
        if funcs:
            return funcs[0]
    raise LookupError(name)


def set_types(bv, signatures: dict[str, str]) -> None:
    for name, signature in signatures.items():
        function_named(bv, name).set_user_type(signature)
    bv.update_analysis_and_wait()


def return_expr(func):
    rets = [i for i in func.mlil.instructions if i.operation == MediumLevelILOperation.MLIL_RET]
    assert len(rets) == 1, rets
    return rets[0], rets[0].src[0]


def lift_return(func, *, check_versions: bool = False):
    instr, expr = return_expr(func)
    return lift(expr.ssa_form, root_instr=instr, check_versions=check_versions, max_nodes=10_000)


def strip_versions(e, params=("x", "y", "z", "p")):
    """Rename leaves that read a parameter to the parameter's name. A backing value (the whole
    register a narrow parameter lives in, binja_d810.binja.lift.Backing) keeps its own width."""
    mapping = {}
    for v in variables(e):
        if isinstance(v.ref, (SSAVariable, Backing)) and v.ref.var.name in params:
            mapping[v.name] = Var(v.ref.var.name, v.width)
    return substitute(e, mapping)


def signature(entry: dict) -> str:
    t = {32: "uint32_t", 64: "uint64_t"}[entry["width"]]
    return f"{t} {entry['name']}({t} x, {t} y, {t} z)"


def data_pointers(bv, func) -> list[int]:
    """Constants in the function's MLIL that point into writable data, ascending: its tables and globals
    (static ones have no symbol in a PE)."""
    from binaryninja import MediumLevelILOperation as M

    found = set()
    stack = list(func.mlil.instructions)
    while stack:
        node = stack.pop()
        if node.operation in (M.MLIL_CONST_PTR, M.MLIL_CONST):
            segment = bv.get_segment_at(node.constant)
            if segment is not None and segment.writable and not segment.executable:
                found.add(node.constant)
        for operand in node.operands:
            if hasattr(operand, "operation"):
                stack.append(operand)
            elif isinstance(operand, list):
                stack.extend(o for o in operand if hasattr(o, "operation"))
    return sorted(found)


def settle(bv, seconds: float = 120) -> None:
    """Wait for the flow fixes (v4.0): analysis idle, and no round queued, armed or running, twice in a row."""
    import time

    try:
        from binja_d810.binja.flow import apply
    except ImportError:  # a plugin revision without the flow fixes (the workbench, back to back with main)
        bv.update_analysis_and_wait()
        return
    deadline = time.monotonic() + seconds
    quiet = 0
    while quiet < 2:
        bv.update_analysis_and_wait()
        quiet = 0 if apply.busy(bv) else quiet + 1
        if time.monotonic() > deadline:
            raise TimeoutError(f"the flow fixes were still busy after {seconds} s")
        time.sleep(0.05)


def marks(func) -> list[int]:
    """The constant each call of mark passes, in HLIL order. An argument in a variable is read by its dataflow
    value: on arm64 mark returns its argument in x0, so Binary Ninja writes x0 = 0xb; _mark(x0)."""
    def passed(node):
        if node.operation != HighLevelILOperation.HLIL_CALL or not node.params \
                or node.dest.operation != HighLevelILOperation.HLIL_CONST_PTR:
            return None
        callee = func.view.get_function_at(node.dest.constant)
        value = node.params[0].value
        if callee is None or callee.name.lstrip("_") != "mark" or value.type not in _CONSTANTS:
            return None
        return value.value
    return [k for instr in func.hlil.instructions for k in instr.traverse(passed)]
