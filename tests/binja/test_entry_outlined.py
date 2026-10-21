"""Outlined handlers must preserve undeclared callback registers through every transition."""

from types import SimpleNamespace

import pytest
from binaryninja import (Function, MediumLevelILFunction, RegisterValueType, Settings, Variable,
                         VariableSourceType)
from flowcases import text
from helpers import function_named, open_sample, set_types, settle

from binja_d810.binja import log, workflow
from binja_d810.binja.unflatten import entry, selfcall, splice

BUILDS = ["entry_outlined.dylib", "entry_outlined_x86_64.so",
          "entry_outlined_x86_64-pe.dll", "entry_outlined_i686-pe.dll"]
NAMES = [f"{group}_{suffix}" for group in ("entry", "volatile", "clobber")
         for suffix in ("dispatch", "h1", "h2", "h3")] + ["entry_forward"]


def _open(file):
    bv = open_sample(file, d810=False)
    set_types(bv, {name: f"uint{bv.address_size * 8}_t {name}(uint32_t state)" for name in NAMES})
    return bv


@pytest.fixture(scope="module", params=BUILDS)
def off(request):
    bv = _open(request.param)
    yield bv
    bv.file.close()


def _targets(mlil):
    return {i.dest.constant for i in mlil.instructions
            if i.operation in selfcall._CALLS | selfcall._TAILS and i.dest.operation in selfcall._CONSTANTS}


def test_handlers_preserving_a_saved_callback_register_can_be_spliced(off):
    func = function_named(off, "entry_dispatch")
    assert splice._reads_entry(func.mlil, func.parameter_vars)
    found = splice.handlers(func.mlil, off)
    assert found is not None, text(func)
    mlil, count = found
    assert count == 3
    assert not _targets(mlil) & {function_named(off, f"entry_h{n}").start for n in (1, 2, 3)}
    assert splice._reads_entry(mlil, func.parameter_vars), "the callback input must remain in the copied body"


def test_a_volatile_callback_register_stays_conservative(off):
    func = function_named(off, "volatile_dispatch")
    assert splice._reads_entry(func.mlil, func.parameter_vars)
    assert splice.handlers(func.mlil, off) is None


def test_a_handler_overwriting_the_callback_is_left_as_a_call(off):
    func = function_named(off, "clobber_dispatch")
    found = splice.handlers(func.mlil, off)
    assert found is not None, text(func)
    mlil, count = found
    assert count == 2
    assert function_named(off, "clobber_h1").start in _targets(mlil)
    assert not _targets(mlil) & {function_named(off, f"clobber_h{n}").start for n in (2, 3)}


@pytest.mark.parametrize("where", ["dispatch", "h1", "forward"])
@pytest.mark.parametrize("evidence", ["unknown", "other_register", "offset"])
def test_missing_or_different_native_entry_values_reject_the_transition(off, monkeypatch, where, evidence):
    original = Function.get_reg_value_at
    target = function_named(off, f"entry_{where}").start

    def value(func, address, reg, *args):
        found = original(func, address, reg, *args)
        if func.start != target:
            return found
        return SimpleNamespace(type=RegisterValueType.UndeterminedValue if evidence == "unknown" else found.type,
                               value=found.value + int(evidence == "other_register"),
                               offset=int(evidence == "offset"))

    monkeypatch.setattr(Function, "get_reg_value_at", value)
    func = function_named(off, "entry_dispatch")
    found = splice.handlers(func.mlil, off)
    if where == "dispatch":
        assert found is None
    else:
        assert found is not None and found[1] == 2
        retained = function_named(off, "entry_h3").start if where == "forward" else target
        assert retained in _targets(found[0])


def _synthetic(func):
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(func.mlil)
    il.prepare_to_copy_block(func.mlil.basic_blocks[0])
    il.set_current_address(func.start, func.arch)
    return il


def _alias(func):
    context = entry.Preserved(func.mlil, func.parameter_vars)
    assert context.supported and len(context.registers) == 1
    (full,) = context.registers
    name, info = next((name, info) for name, info in func.arch.regs.items()
                      if info.full_width_reg == full and name != full and info.offset == 0)
    return Variable(func, VariableSourceType.RegisterVariableSourceType, 0, func.arch.get_reg_index(name)), info.size


@pytest.mark.parametrize("mode", ["stack", "partial_register"])
def test_undeclared_stack_and_partial_register_inputs_stay_unsupported(off, mode):
    func = function_named(off, "entry_dispatch")
    var, size = (Variable(func, VariableSourceType.StackVariableSourceType, 0, -0x1000), 4) \
        if mode == "stack" else _alias(func)
    il = _synthetic(func)
    il.append(il.ret([il.var(size, var)]))
    il.finalize()
    il.generate_ssa_form()
    assert splice._reads_entry(il, func.parameter_vars)
    assert not entry.Preserved(il, func.parameter_vars).supported


def test_a_handler_variable_aliasing_the_preserved_register_rejects(off):
    func = function_named(off, "entry_dispatch")
    context = entry.Preserved(func.mlil, func.parameter_vars)
    handler = function_named(off, "entry_h1")
    var, size = _alias(func)
    var = Variable(handler, var.source_type, var.index, var.storage)
    il = _synthetic(handler)
    il.append(il.set_var(size, var, il.const(size, 1)))
    il.append(il.ret([il.var(size, var)]))
    il.finalize()
    il.generate_ssa_form()
    assert il.get_var_definitions(var)
    assert not context.by(il)


@pytest.mark.parametrize("file", BUILDS)
def test_recovery_keeps_the_callback_and_disabling_restores_the_original(file):
    bv = _open(file)
    try:
        func = function_named(bv, "entry_dispatch")
        before = text(func)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        func = function_named(bv, "entry_dispatch")
        report = [line for r in log.latest_reports(func).values() for line in r.unflatten]
        assert any("3 handlers spliced" in line for line in report), (text(func), report)
        assert not any(f"entry_h{n}(" in text(func) for n in (1, 2, 3)), text(func)
        assert splice._reads_entry(func.mlil, func.parameter_vars), text(func)
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        assert text(function_named(bv, "entry_dispatch")) == before
    finally:
        bv.file.close()
