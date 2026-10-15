"""Shared helpers for tests that run inside Binary Ninja."""

import json
from pathlib import Path

import binaryninja
import pytest
from binaryninja import MediumLevelILOperation, SSAVariable

from binja_d810.binja import workflow
from binja_d810.binja.mlil.lift import Backing, lift
from binja_d810.ir.expr import Var, substitute, variables

BUILD = Path(__file__).resolve().parents[1] / "samples" / "build"


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
