"""Private stack cleanup: real native IL, independent value models, and negative controls."""

from dataclasses import replace

import pytest
from binaryninja import (ILInstructionAttribute as Attr, MediumLevelILFunction,
                         MediumLevelILOperation as Op, Settings, SSAVariable, Type, Variable, VariableSourceType)
from arch import IDS, MATRIX, sample
from helpers import function_named, lift_return, open_sample, set_types, settle
from flowcases import text

from binja_d810.binja import log, settings, scratch
from binja_d810.binja.mlil.lift import Backing
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import BinOp, Const, UnOp, Var, substitute, variables
from binja_d810.semantic.verify import Verdict, check_equivalent

VALUES = ("scratch_dead", "scratch_roundtrip", "scratch_encoded_address", "scratch_read", "scratch_branch", "scratch_loop", "scratch_partial")
TYPES = {name: f"uint32_t {name}(uint32_t x)" for name in (*VALUES, "scratch_escape")}
TYPES["scratch_unknown"] = "uint32_t scratch_unknown(uint32_t x, void (*f)(uint32_t))"


def value_ir(func):
    expr = lift_return(func, check_versions=False)
    assert expr is not None, text(func)
    parameter = func.parameter_vars[0]
    inputs = [v for v in variables(expr) if isinstance(v.ref, (SSAVariable, Backing))
              and v.ref.var.identifier == parameter.identifier and v.ref.version == 0]
    width = max([32] + [v.width for v in inputs])
    source = Var("scratch_input", width)
    expr = substitute(expr, {v.name: source if v.width == width else UnOp("trunc", source, v.width) for v in inputs})
    return expr, source if width == 32 else UnOp("trunc", source, 32)


def reference(name, x):
    c = lambda n: Const(n, 32)
    if name == "scratch_dead": return BinOp("add", x, c(11), 32)
    if name == "scratch_roundtrip": return BinOp("xor", x, c(49), 32)
    if name == "scratch_encoded_address": return BinOp("xor", x, c(81), 32)
    if name == "scratch_read": return BinOp("add", BinOp("mul", x, c(9), 32), c(1), 32)
    if name == "scratch_branch": return BinOp("xor", x, c(19), 32)
    if name == "scratch_loop": return BinOp("xor", x, c(41), 32)
    return BinOp("or", BinOp("and", x, c(0xffffff00), 32), c(0x6b), 32)


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    off = open_sample(sample("scratch", *request.param), d810=True, options={settings.SCRATCH: False})
    on = open_sample(sample("scratch", *request.param), d810=True)
    for bv in (off, on):
        set_types(bv, TYPES)
        settle(bv)
    yield request.param, on, off
    on.file.close()
    off.file.close()


@pytest.mark.parametrize("name", VALUES)
def test_value_is_proven_against_an_independent_model(builds, request, name):
    build, on, _ = builds
    if name == "scratch_partial" and build == ("i686-pe", "O2"):
        request.applymarker(pytest.mark.xfail(strict=True, reason=
            "native MLIL already folds a byte store followed by a four-byte stack load to 0x6b, losing the upper bytes before D810"))
    expr, x = value_ir(function_named(on, name))
    assert expr.width == 32
    assert check_equivalent(expr, reference(name, x)) is Verdict.PROVEN, text(function_named(on, name))


@pytest.mark.parametrize("name", ("scratch_dead", "scratch_roundtrip", "scratch_branch", "scratch_loop"))
def test_dead_scratch_traffic_is_removed(builds, name):
    _, on, off = builds
    func = function_named(on, name)
    report = log.latest_report(func)
    assert report.scratch_stores > 0, report.scratch
    assert len(list(func.mlil.instructions)) >= 1
    assert text(func) != text(function_named(off, name))
    assert "scratch:" in log.render(report)


def test_escaping_slot_is_left_unchanged(builds):
    _, on, off = builds
    func = function_named(on, "scratch_escape")
    assert text(func) == text(function_named(off, "scratch_escape"))
    report = log.latest_report(func)
    assert not report.scratch_stores and not report.scratch_accesses and not report.scratch_aliases


def test_an_unknown_call_keeps_the_memory_barrier(builds):
    _, on, off = builds
    func = function_named(on, "scratch_unknown")
    def calls(f):
        return sum("CALL" in i.operation.name for i in f.mlil.instructions)
    assert calls(func) == calls(function_named(off, "scratch_unknown")) == 1
    assert any("unknown call" in n for n in log.latest_report(func).scratch)


@pytest.mark.parametrize("limit", ("instructions", "nodes", "frame_bytes"))
def test_named_caps_leave_unproved_code_unchanged(builds, limit):
    _, _, off = builds
    original = function_named(off, "scratch_dead").mlil
    changed, report = scratch.run(original, replace(scratch.Limits(), **{limit: 1}))
    assert changed is original and report.notes
    assert not report.stores and not report.accesses and not report.aliases


def test_an_expired_shared_budget_cannot_commit(builds):
    _, _, off = builds
    original = function_named(off, "scratch_dead").mlil
    changed, report = scratch.run(original, deadline=Deadline.after(0))
    assert changed is original and report.exhausted
    assert any("budget" in n for n in report.notes)


def local_program(func, mode):
    """Native IL controls with explicit bytes, independent of compiler stack allocation."""
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(func.mlil)
    il.prepare_to_copy_block(func.mlil.basic_blocks[0])
    il.set_current_address(func.start, func.arch)
    slot = Variable(func, VariableSourceType.StackVariableSourceType, 0, -0x2000)
    value = il.set_var(4, slot, il.const(4, 0x12345678))
    if isinstance(mode, Attr):
        il.set_expr_attributes(value, {mode})
    il.append(value)
    if mode == "partial":
        il.append(il.set_var_field(1, slot, 0, il.const(1, 0x6b)))
        il.append(il.ret([il.var(4, slot)]))
    else:
        il.append(il.ret([il.const(4, 17)]))
    il.finalize()
    il.generate_ssa_form()
    return il


@pytest.mark.parametrize("attribute", (Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis))
def test_native_protected_stores_survive(builds, attribute):
    _, _, off = builds
    original = local_program(function_named(off, "scratch_dead"), attribute)
    changed, report = scratch.run(original)
    assert changed is original and report.stores == 0
    assert any("protected" in n for n in report.notes)


def test_liveness_cap_never_returns_partial_deadness(builds):
    _, _, off = builds
    original = local_program(function_named(off, "scratch_dead"), "dead")
    changed, report = scratch.run(original, scratch.Limits(visits=1))
    assert changed is original and report.stores == 0
    assert "liveness budget exhausted" in report.notes
    changed, report = scratch.run(original)
    assert changed is not original and report.stores == 1


def test_native_partial_overwrite_preserves_the_older_live_bytes(builds):
    _, _, off = builds
    original = local_program(function_named(off, "scratch_dead"), "partial")
    changed, report = scratch.run(original)
    assert report.stores == 0
    assert sum(i.operation in (Op.MLIL_SET_VAR, Op.MLIL_SET_VAR_FIELD) for i in changed.instructions) == 2


def test_dead_store_cleanup_preserves_native_call_outputs(builds):
    _, _, off = builds
    func = function_named(off, "scratch_dead")
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(func.mlil)
    il.prepare_to_copy_block(func.mlil.basic_blocks[0])
    il.set_current_address(func.start, func.arch)
    slot = Variable(func, VariableSourceType.StackVariableSourceType, 0, -0x2000)
    output = func.parameter_vars[0]
    il.append(il.call([il.var(4, output)], il.const_pointer(off.address_size, func.start), []))
    il.append(il.set_var(4, slot, il.const(4, 29)))
    il.append(il.ret([il.var(4, output)]))
    il.finalize()
    il.generate_ssa_form()
    changed, report = scratch.run(il)
    assert report.stores == 1 and changed is not il
    calls = [i for i in changed.instructions if i.operation == Op.MLIL_CALL]
    assert len(calls) == 1 and calls[0].dest.constant == func.start
    assert calls[0].output == il[0].output
    assert [(i.size, i.operation) for i in calls[0].output_exprs] == [(4, Op.MLIL_VAR)]


@pytest.mark.parametrize("volatile", (False, True))
def test_native_alias_promotion_requires_an_ordinary_private_slot(builds, volatile):
    _, _, off = builds
    func = function_named(off, "scratch_dead")
    slot = Variable(func, VariableSourceType.StackVariableSourceType, 0, -0x3000)
    kind = Type.int(4, False).mutable_copy()
    kind.volatile = volatile
    func.create_user_var(slot, kind.immutable_copy(), "scratch_test_slot")
    settle(off)
    assert slot.type is not None and slot.type.width == 4
    assert bool(slot.type.volatile) == volatile
    try:
        il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
        il.prepare_to_copy_function(func.mlil)
        il.prepare_to_copy_block(func.mlil.basic_blocks[0])
        il.set_current_address(func.start, func.arch)
        il.append(il.set_var(4, slot, il.const(4, 0)))
        il.append(il.store(4, il.address_of(slot), il.const(4, 0x12345678)))
        il.append(il.ret([il.load(4, il.address_of(slot))]))
        il.finalize()
        il.generate_ssa_form()
        assert slot in il.ssa_form.aliased_vars
        changed, report = scratch.run(il)
        if volatile:
            assert changed is il and not report.stores and not report.accesses and not report.aliases
        else:
            assert report.accesses == 2 and report.aliases == 1
            assert slot not in changed.ssa_form.aliased_vars
            assert not any(n.operation in scratch._LOADS | scratch._STORES
                           for i in changed.instructions for n in scratch.walk(i))
            from binja_d810.binja.mlil.lift import lift
            ret = next(i for i in changed.instructions if i.operation == Op.MLIL_RET)
            value = lift(ret.src[0].ssa_form, root_instr=ret, check_versions=False)
            assert check_equivalent(value, Const(0x12345678, 32)) is Verdict.PROVEN
    finally:
        func.delete_user_var(slot)


def test_native_i686_partial_store_limitation_is_not_introduced_by_d810():
    views = [open_sample("scratch_i686-pe_O2.dll", d810=False),
             open_sample("scratch_i686-pe_O2.dll", d810=True)]
    try:
        for bv in views:
            set_types(bv, {"scratch_partial": TYPES["scratch_partial"]})
            settle(bv)
        before, _ = value_ir(function_named(views[0], "scratch_partial"))
        after, _ = value_ir(function_named(views[1], "scratch_partial"))
        assert check_equivalent(before, after) is Verdict.PROVEN
    finally:
        for bv in views: bv.file.close()


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_disabling_scratch_restores_the_original_il(build):
    bv = open_sample(sample("scratch", *build), d810=True, options={settings.SCRATCH: False})
    try:
        set_types(bv, TYPES)
        settle(bv)
        before = {name: text(function_named(bv, name)) for name in VALUES}
        Settings().set_bool(settings.SCRATCH, True, bv)
        for f in bv.functions: f.reanalyze()
        settle(bv)
        Settings().set_bool(settings.SCRATCH, False, bv)
        for f in bv.functions: f.reanalyze()
        settle(bv)
        assert {name: text(function_named(bv, name)) for name in VALUES} == before
    finally:
        bv.file.close()
