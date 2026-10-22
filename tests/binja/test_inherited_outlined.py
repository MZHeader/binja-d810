"""Handlers inheriting declared register inputs and tail branches sharing stack arguments."""

from types import SimpleNamespace

import pytest
from binaryninja import (Function, FunctionParameter, ILSourceLocation, MediumLevelILFunction,
                         MediumLevelILOperation as Op, RegisterValueType, Settings, Type, Variable, VariableSourceType)
from flowcases import text
from helpers import function_named, open_sample, settle

from binja_d810.binja import log, workflow
from binja_d810.binja.ssa import definition_of
from binja_d810.binja.unflatten import incoming, selfcall, splice
from binja_d810.binja.unflatten.graph import MLILGraph
from binja_d810.unflatten.thread import plan

BUILDS = ["inherited_outlined.dylib", "inherited_outlined_x86_64.so",
          "inherited_outlined_x86_64-pe.dll", "inherited_outlined_i686-pe.dll"]


def _open(file):
    bv = open_sample(file, d810=False)
    saved = {"aarch64": "x21", "x86_64": "r12", "x86": "esi"}[bv.arch.name]
    for group in ("incoming", "changed", "shifted"):
        for n in (1, 2, 3):
            function_named(bv, f"{group}_h{n}").set_user_type(f"uint{bv.address_size * 8}_t h(void)")
        func = function_named(bv, f"{group}_dispatch")
        var = Variable(func, VariableSourceType.RegisterVariableSourceType, 0, bv.arch.get_reg_index(saved))
        stack = Variable(func, VariableSourceType.StackVariableSourceType, 0, 0x20)
        func.set_user_type(Type.function(Type.int(bv.address_size, False),
            [FunctionParameter(Type.int(bv.address_size, False), "payload", var),
             FunctionParameter(Type.int(4, False), "state", stack)],
            calling_convention=func.calling_convention))
    bv.update_analysis_and_wait()
    return bv


@pytest.fixture(scope="module", params=BUILDS)
def off(request):
    bv = _open(request.param)
    yield bv
    bv.file.close()


def _targets(mlil):
    return {i.dest.constant for i in mlil.instructions
            if i.operation in selfcall._CALLS | selfcall._TAILS and i.dest.operation in selfcall._CONSTANTS}


def _sparse_calls(bv, monkeypatch):
    """Retain native handler IL generated before the dispatcher's stack parameter was known."""
    func = function_named(bv, "incoming_dispatch")
    complete = func.type
    func.set_user_type(Type.function(Type.int(bv.address_size, False), [complete.parameters[0]],
                                    calling_convention=func.calling_convention))
    bv.update_analysis_and_wait()
    snapshots = {function_named(bv, f"incoming_h{n}").start: function_named(bv, f"incoming_h{n}").mlil
                 for n in (1, 2, 3)}
    assert all(any(len(i.params) == 1 for i in il.instructions if i.operation in selfcall._TAILS)
               for il in snapshots.values())
    func.set_user_type(complete)
    bv.update_analysis_and_wait()
    original = Function.mlil

    def mlil(f):
        if f.view == bv and f.start in snapshots:
            return snapshots[f.start]
        return original.fget(f)

    monkeypatch.setattr(Function, "mlil", property(mlil))
    return snapshots


def test_omitted_stack_arguments_are_completed_only_after_frame_proof(off, monkeypatch):
    snapshots = _sparse_calls(off, monkeypatch)
    func = function_named(off, "incoming_dispatch")
    found = splice.handlers(func.mlil, off)
    assert found is not None and found[1] == 3
    callbacks = [i for i in found[0].instructions if i.operation in selfcall._TAILS
                 and i.dest.operation in selfcall._CONSTANTS and i.dest.constant == func.start]
    assert len(callbacks) == 3 and all(len(i.params) == 2 for i in callbacks)
    states = set()
    for call in callbacks:
        state = call.ssa_form.params[-1]
        assert state.operation == Op.MLIL_VAR_SSA and state.src.version != 0
        definition = definition_of(found[0].ssa_form, state.src, {})
        assert definition.operation == Op.MLIL_SET_VAR_SSA and definition.src.operation == Op.MLIL_CONST
        states.add(definition.src.constant)
    assert states == {22, 33, 44}
    looped = selfcall.loop(found[0], off)
    assert looped is not None and looped[2] == 3
    graph = MLILGraph(looped[0], view=off, ranges=[], passed=frozenset(func.parameter_vars))
    assert plan(graph).threads
    assert all(len(il) >= 2 for il in snapshots.values()), "the retained IL must include the stack stores"


def _rejected(off, side):
    func = function_named(off, "incoming_dispatch")
    found = splice.handlers(func.mlil, off)
    if side == "caller":
        assert found is None
    else:
        assert found is not None and found[1] == 2
        assert function_named(off, "incoming_h1").start in _targets(found[0])


@pytest.mark.parametrize("side", ["caller", "callee"])
@pytest.mark.parametrize("evidence", ["unknown", "other_register", "frame", "offset"])
def test_missing_or_changed_register_evidence_keeps_the_call(off, monkeypatch, side, evidence):
    _sparse_calls(off, monkeypatch)
    caller = function_named(off, "incoming_dispatch")
    target = caller.start if side == "caller" else function_named(off, "incoming_h1").start
    storage = list(caller.parameter_vars)[0].storage
    original = Function.get_reg_value_at

    def value(func, address, reg, *args):
        result = original(func, address, reg, *args)
        if func.start != target or func.arch.get_reg_index(reg) != storage:
            return result
        kind = {"unknown": RegisterValueType.UndeterminedValue, "frame": RegisterValueType.StackFrameOffset}.get(
            evidence, result.type)
        return SimpleNamespace(type=kind, value=result.value + int(evidence == "other_register"),
                               offset=int(evidence == "offset"))

    monkeypatch.setattr(Function, "get_reg_value_at", value)
    _rejected(off, side)


@pytest.mark.parametrize("side", ["caller", "callee"])
@pytest.mark.parametrize("evidence", ["stack", "instruction", "override"])
def test_shared_stack_arguments_need_both_native_tail_branches(off, monkeypatch, side, evidence):
    _sparse_calls(off, monkeypatch)
    caller = function_named(off, "incoming_dispatch")
    target = caller.start if side == "caller" else function_named(off, "incoming_h1").start
    if evidence == "stack":
        original = Function.get_reg_value_at
        def value(func, address, reg, *args):
            if func.start == target and reg == func.arch.stack_pointer:
                return SimpleNamespace(type=RegisterValueType.StackFrameOffset, value=-16, offset=0)
            return original(func, address, reg, *args)
        monkeypatch.setattr(Function, "get_reg_value_at", value)
    elif evidence == "instruction":
        original = incoming.branch_instruction
        monkeypatch.setattr(incoming, "branch_instruction", lambda f, a: None if f.start == target else original(f, a))
    else:
        original = Function.get_call_type_adjustment
        monkeypatch.setattr(Function, "get_call_type_adjustment",
                            lambda f, a, *args: f.type if f.start == target else original(f, a, *args))
    _rejected(off, side)


@pytest.mark.parametrize("size", [0, 8])
def test_unknown_or_overlapping_stack_widths_keep_the_call(off, monkeypatch, size):
    snapshots = _sparse_calls(off, monkeypatch)
    handler = function_named(off, "incoming_h1")
    original = incoming._size
    def width(var):
        if var.function == handler and var.source_type == VariableSourceType.StackVariableSourceType:
            return size
        return original(var)
    monkeypatch.setattr(incoming, "_size", width)
    assert not incoming.Inputs(function_named(off, "incoming_dispatch"), snapshots[handler.start]).supported
    _rejected(off, "callee")


def test_modified_caller_ssa_does_not_reuse_native_entry_evidence(off, monkeypatch):
    _sparse_calls(off, monkeypatch)
    func = function_named(off, "incoming_dispatch")
    old = func.mlil
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(old)
    payload = list(func.parameter_vars)[0]
    for block in old.basic_blocks:
        il.prepare_to_copy_block(block)
        if block.start == 0:
            il.set_current_address(old[0].address, func.arch)
            il.append(il.set_var(off.address_size, payload, il.const(off.address_size, 1)))
        for instr in block:
            il.set_current_address(instr.address, func.arch)
            il.append(instr.copy_to(il), ILSourceLocation.from_instruction(instr))
    il.finalize()
    il.generate_ssa_form()
    assert il.get_var_definitions(payload)
    assert splice.handlers(il, off) is None


@pytest.mark.parametrize("mode", ["load", "store", "address", "call"])
def test_implicit_stack_arguments_reject_memory_aliases_and_helpers(off, monkeypatch, mode):
    snapshots = _sparse_calls(off, monkeypatch)
    func = function_named(off, "incoming_h1")
    old = snapshots[func.start]
    slot = next(v for v in old.vars if v.source_type == VariableSourceType.StackVariableSourceType)
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(old)
    for block in old.basic_blocks:
        il.prepare_to_copy_block(block)
        if block.start == 0:
            il.set_current_address(old[0].address, func.arch)
            ptr = il.const_pointer(off.address_size, off.start)
            if mode == "load":
                il.append(il.set_var(4, slot, il.load(4, ptr)))
            elif mode == "store":
                il.append(il.store(4, ptr, il.const(4, 1)))
            elif mode == "address":
                address = il.address_of(slot)
                if off.address_size > 4:
                    address = il.low_part(4, address)
                il.append(il.set_var(4, slot, address))
            else:
                il.append(il.call([], il.const_pointer(off.address_size, function_named(off, "incoming_dispatch").start), []))
        for instr in block:
            il.set_current_address(instr.address, func.arch)
            il.append(instr.copy_to(il), ILSourceLocation.from_instruction(instr))
    il.finalize()
    il.generate_ssa_form()
    snapshots[func.start] = il
    assert not incoming.Inputs(function_named(off, "incoming_dispatch"), il).supported
    _rejected(off, "callee")


def test_inherited_register_parameters_and_shared_stack_state_are_recovered(off):
    func = function_named(off, "incoming_dispatch")
    assert len(func.parameter_vars) == 2, list(func.parameter_vars)
    handler = function_named(off, "incoming_h1")
    assert splice._reads_entry(handler.mlil, [])
    assert any(len(i.params) == 2 for i in handler.mlil.instructions if i.operation in selfcall._TAILS)
    found = splice.handlers(func.mlil, off)
    assert found is not None and found[1] == 3, text(func)
    callbacks = [i for i in found[0].instructions if i.operation in selfcall._TAILS
                 and i.dest.operation in selfcall._CONSTANTS and i.dest.constant == func.start]
    assert len(callbacks) == 3 and all(len(i.params) == 2 for i in callbacks)


def test_a_changed_incoming_register_keeps_its_handler_call(off):
    func = function_named(off, "changed_dispatch")
    found = splice.handlers(func.mlil, off)
    assert found is not None and found[1] == 2, text(func)
    assert function_named(off, "changed_h1").start in _targets(found[0])


def test_different_stack_frames_are_not_treated_as_shared_arguments(off):
    func = function_named(off, "shifted_dispatch")
    assert splice.handlers(func.mlil, off) is None


@pytest.mark.parametrize("file", BUILDS)
def test_workflow_recovery_and_undo_keep_the_incoming_payload(file):
    bv = _open(file)
    try:
        func = function_named(bv, "incoming_dispatch")
        before = text(func)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        func = function_named(bv, "incoming_dispatch")
        report = [line for r in log.latest_reports(func).values() for line in r.unflatten]
        assert any("3 handlers spliced" in line for line in report), (text(func), report)
        assert not any(f"incoming_h{n}(" in text(func) for n in (1, 2, 3)), text(func)
        assert "payload" in text(func), text(func)
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        assert text(function_named(bv, "incoming_dispatch")) == before
    finally:
        bv.file.close()
