"""Original flag semantics, protected/data uses, liveness and shared source limits."""
from dataclasses import replace
import pytest
from binaryninja import (FlagRole, ILInstructionAttribute as Attr, LLIL_TEMP,
                         LowLevelILFlagCondition as C, LowLevelILFunction, LowLevelILLabel, LowLevelILOperation as L, Settings)
from helpers import function_named, open_sample, set_types, settle
from flowcases import text
from flag_model import proves
from test_flag_conditions import builds, TYPES
from binja_d810.binja import source_flags, settings, log
from binja_d810.flags.recombine import Limits
from binja_d810.deadline import Deadline


def program(func, mode, size=4):
    arch = func.arch
    writer = next(k for k,v in arch.flags_written_by_flag_write_type.items()
                  if arch.semantic_class_for_flag_write_type[k] in source_flags.INTEGER_CLASSES[arch.name]
                  and any(arch.get_flag_role(f, arch.semantic_class_for_flag_write_type[k]) == FlagRole.OverflowFlagRole for f in v))
    overflow = next(f for f in arch.flags_written_by_flag_write_type[writer]
                    if arch.get_flag_role(f, arch.semantic_class_for_flag_write_type[writer]) == FlagRole.OverflowFlagRole)
    il = LowLevelILFunction(arch, source_func=func)
    il.prepare_to_copy_function(func.lifted_il)
    il.prepare_to_copy_block(func.lifted_il.basic_blocks[0])
    il.set_current_address(func.start, arch)
    a = il.load(size, il.reg(arch.address_size, arch.stack_pointer)) if mode == 'memory' else il.const(size, 0)
    if mode == 'clobber':
        il.append(il.set_reg(size, LLIL_TEMP(0), a)); a = il.reg(size, LLIL_TEMP(0))
    operation = il.sub(size, a, il.const(size, 1 << (size * 8 - 1)), flags=writer)
    if mode == 'clobber': operation = il.set_reg(size, LLIL_TEMP(0), operation)
    if mode == 'protected': il.set_expr_attributes(operation, {Attr.ILPreventDeadStoreElimination})
    il.append(operation)
    if mode == 'dead': il.append(il.sub(size, il.const(size, 3), il.const(size, 1 << (size * 8 - 1)), flags=writer))
    if mode == 'raw': il.append(il.set_reg(1, LLIL_TEMP(0), il.bool_to_int(1, il.flag(overflow))))
    yes, no = LowLevelILLabel(), LowLevelILLabel()
    cls = arch.semantic_class_for_flag_write_type[writer]
    # ARM64 declares dependencies on semantic groups, not FLAG_COND operations.
    group = next((g for g, deps in arch.flags_required_for_semantic_flag_group.items() if overflow in deps), None)
    if group is not None:
        condition = il.flag_group(group)
    else:
        code = next(c for c in C if overflow in arch.get_flags_required_for_flag_condition(c, cls))
        condition = il.flag_condition(code, cls)
    il.append(il.if_expr(condition, yes, no))
    il.mark_label(yes); il.append(il.ret(il.const_pointer(arch.address_size, func.start)))
    il.mark_label(no); il.append(il.ret(il.const_pointer(arch.address_size, func.start)))
    il.finalize()
    return il


def explicit_flags(il):
    """Independent readback of emitted scalar arithmetic and virtual register captures."""
    regs, flags = {}, {}
    def value(node):
        mask = (1 << (node.size * 8 or 1)) - 1
        if node.operation == L.LLIL_CONST: return node.constant & mask
        if node.operation == L.LLIL_REG: return regs[str(node.src)] & mask
        a, b = value(node.left), value(node.right)
        return {L.LLIL_SUB: lambda: a-b, L.LLIL_AND: lambda: a&b, L.LLIL_XOR: lambda: a^b,
                L.LLIL_CMP_NE: lambda: int(a!=b)}[node.operation]() & mask
    for instr in il.instructions:
        if instr.operation == L.LLIL_SET_REG: regs[str(instr.dest)] = value(instr.src)
        if instr.operation == L.LLIL_SET_FLAG: flags[str(instr.dest)] = value(instr.src)
    return flags


@pytest.mark.parametrize('mode', ('positive', 'dead', 'clobber', 'raw', 'memory', 'protected'))
def test_source_models_preserve_effect_and_flag_use_boundaries(builds, mode):
    _, _, off = builds
    il = program(function_named(off, 'flag_fixed_chain'), mode)
    new, report = source_flags.run(il, Limits())
    if mode in ('positive', 'dead', 'clobber'):
        assert new is not il and report.models == 1, report.notes
        assert new.temp_reg_count == il.temp_reg_count + 2
        assert set(explicit_flags(new).values()) == {1}
    else:
        assert new is il and report.models == 0


@pytest.mark.parametrize('size', (1,2,4,8))
def test_native_capture_and_constant_widths_preserve_signed_minimum(builds, size):
    _, _, off = builds
    il = program(function_named(off, 'flag_fixed_chain'), 'clobber', size)
    new, report = source_flags.run(il, Limits())
    assert report.models == 1 and set(explicit_flags(new).values()) == {1}


def test_source_limits_and_expired_deadline_keep_the_original_il(builds):
    _, _, off = builds
    il = program(function_named(off, 'flag_fixed_chain'), 'positive')
    for key in ('instructions', 'work', 'nodes'):
        new, report = source_flags.run(il, replace(Limits(), **{key: 1}))
        assert new is il and not report.models and report.notes
    for kwargs in ({'max_bits': 16}, {'deadline': Deadline.after(0)}):
        new, report = source_flags.run(il, Limits(), **kwargs)
        assert new is il and not report.models and report.notes


def test_signed_chain_rounds_and_disable_work_without_callee_summaries():
    bv = open_sample('flag_conditions_O2.dylib', d810=True,
                     options={settings.CALLEES: False, settings.FLAG_SOURCE: False})
    try:
        set_types(bv, TYPES); settle(bv)
        func = function_named(bv, 'flag_fixed_chain')
        before = text(func); assert 'add_overflow' in before
        Settings().set_bool(settings.FLAG_SOURCE, True, bv)
        Settings().set_integer(settings.FLAG_ROUNDS, 1, bv)
        func.reanalyze(); settle(bv)
        assert proves(func, 'flag_fixed_chain') and 'add_overflow' not in text(func)
        assert log.latest_report(func).flag_sources > 0
        Settings().set_integer(settings.FLAG_ROUNDS, 3, bv)
        func.reanalyze(); settle(bv)
        assert proves(func, 'flag_fixed_chain') and len(list(func.hlil.instructions)) == 1
        Settings().set_bool(settings.FLAG_SOURCE, False, bv)
        func.reanalyze(); settle(bv)
        assert text(func) == before
    finally: bv.file.close()
