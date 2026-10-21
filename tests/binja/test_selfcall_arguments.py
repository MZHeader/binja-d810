"""Frame checks over synthetic MLIL with many ABI parameters, across the native build matrix."""

import pytest
from arch import IDS, MATRIX, sample
from binaryninja import MediumLevelILFunction, MediumLevelILLabel, Variable, VariableSourceType
from helpers import function_named, open_sample, set_types

from binja_d810.binja.unflatten import selfcall

COUNT = 48


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def wide(request):
    bv = open_sample(sample("outlined", *request.param), d810=False)
    # Only the function's ABI parameter locations are used by the synthetic MLIL below.
    signature = "uint64_t sc_dispatch(" + ", ".join(f"uint64_t p{i}" for i in range(COUNT)) + ")"
    set_types(bv, {"sc_dispatch": signature})
    func = function_named(bv, "sc_dispatch")
    assert len(func.parameter_vars) == COUNT
    yield func
    bv.file.close()


def _call(func, mode="passthrough"):
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(func.mlil)
    il.prepare_to_copy_block(func.mlil.basic_blocks[0])
    il.set_current_address(func.start, func.arch)
    params = list(func.parameter_vars)
    args = [il.const(8, 7), *[il.var(8, p) for p in params[1:]]]
    local = Variable(func, VariableSourceType.StackVariableSourceType, 0, -0x1000)
    if mode == "assigned":
        for n, param in enumerate(params):
            il.append(il.set_var(8, param, il.const(8, n + 1)))
    elif mode == "address":
        args[-1] = il.address_of(local)
    elif mode in ("reassigned", "overwritten", "partial", "memory"):
        address = il.address_of(local)
        if func.arch.address_size < 8:
            address = il.zero_extend(8, address)
        il.append(il.set_var(8, params[-1], address))
        if mode == "overwritten":
            il.append(il.set_var(8, params[-1], il.var(8, params[-2])))
        elif mode == "partial":
            il.append(il.set_var_field(1, params[-1], 0, il.const(1, 0)))
        elif mode == "memory":
            cell = il.const_pointer(func.arch.address_size, func.view.start)
            il.append(il.store(8, cell, il.var(8, params[-1])))
            il.append(il.set_var(8, params[-1], il.load(8, cell)))
    elif mode == "merged":
        local_arm, input_arm, join = MediumLevelILLabel(), MediumLevelILLabel(), MediumLevelILLabel()
        il.append(il.if_expr(il.compare_equal(8, il.var(8, params[1]), il.const(8, 0)), local_arm, input_arm))
        il.mark_label(local_arm)
        address = il.address_of(local)
        if func.arch.address_size < 8:
            address = il.zero_extend(8, address)
        il.append(il.set_var(8, params[-1], address))
        il.append(il.goto(join))
        il.mark_label(input_arm)
        il.append(il.set_var(8, params[-1], il.var(8, params[-2])))
        il.append(il.goto(join))
        il.mark_label(join)
    elif mode == "returned":
        result = Variable(func, VariableSourceType.StackVariableSourceType, 0, -0x3000)
        # An unknown helper may return its pointer argument, as memcpy and identity helpers do.
        il.append(il.call([result], il.const_pointer(func.arch.address_size, func.start + 4), [il.address_of(local)]))
        args[-1] = il.var(8, result)
    elif mode == "deep":
        previous = params[1]
        for n in range(selfcall.MAX_TRACE + 1):
            var = Variable(func, VariableSourceType.StackVariableSourceType, 0, -0x2000 - 8 * n)
            il.append(il.set_var(8, var, il.var(8, previous)))
            previous = var
        args[-1] = il.var(8, previous)
    il.append(il.tailcall([], il.const_pointer(func.arch.address_size, func.start), args))
    il.finalize()
    il.generate_ssa_form()
    assert list(il.basic_blocks), "the synthetic function must have a valid entry block"
    if mode in ("reassigned", "overwritten", "partial", "merged", "memory"):
        assert il.get_var_definitions(params[-1])
    elif mode == "deep":
        assert il.get_var_definitions(previous)
    elif mode == "returned":
        assert il.get_var_definitions(result)
    return il, params


@pytest.mark.parametrize("mode", ["passthrough", "assigned", "overwritten"])
def test_many_independent_parameters_do_not_exhaust_the_frame_check(wide, mode):
    il, params = _call(wide, mode)
    moves = selfcall.moves(il, len(il) - 1, params)
    assert moves is not None and len(moves) == 1
    assert moves[0][0] == params[0]
    assert selfcall.loop(il, wide.view) is not None


@pytest.mark.parametrize("mode", ["address", "reassigned", "returned", "deep", "merged", "partial", "memory"])
def test_late_frame_addresses_and_deep_definition_chains_stay_calls(wide, mode):
    il, params = _call(wide, mode)
    assert selfcall.moves(il, len(il) - 1, params) is None
    assert selfcall.loop(il, wide.view) is None


def test_shared_argument_expressions_use_their_call_sites_ssa_versions(wide):
    il = MediumLevelILFunction(wide.arch, low_level_il=wide.llil)
    il.prepare_to_copy_function(wide.mlil)
    il.prepare_to_copy_block(wide.mlil.basic_blocks[0])
    il.set_current_address(wide.start, wide.arch)
    params = list(wide.parameter_vars)
    args = [il.const(8, 7), *[il.var(8, p) for p in params[1:]]]
    il.append(il.call([], il.const_pointer(wide.arch.address_size, wide.start + 4), args))
    local = Variable(wide, VariableSourceType.StackVariableSourceType, 0, -0x1000)
    address = il.address_of(local)
    if wide.arch.address_size < 8:
        address = il.zero_extend(8, address)
    il.append(il.set_var(8, params[-1], address))
    il.append(il.tailcall([], il.const_pointer(wide.arch.address_size, wide.start), args))
    il.finalize()
    il.generate_ssa_form()
    assert selfcall.moves(il, 0, params) is not None
    assert selfcall.moves(il, len(il) - 1, params) is None
