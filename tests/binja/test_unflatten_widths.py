"""Mixed-width writes to an untyped copied variable must keep their own SSA widths."""

from types import SimpleNamespace

import pytest
from arch import IDS, MATRIX, sample
from binaryninja import (MediumLevelILFunction, MediumLevelILLabel, MediumLevelILOperation as Op,
                         Variable, VariableSourceType)
from helpers import function_named, open_sample

from binja_d810.binja import unflatten
from binja_d810.binja.unflatten.graph import MLILGraph
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import Const
from binja_d810.unflatten.graph import Assign, Unknown
from binja_d810.unflatten.thread import plan

STATES = (0x19, 0x2d, 0x47)
WIDE = 0x1122334455667788


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def off(request):
    bv = open_sample(sample("mixed_outlined", *request.param), d810=False)
    yield bv
    bv.file.close()


def _machine(bv, mode="constant"):
    func = function_named(bv, "mixed_dispatch")
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(func.mlil)
    il.prepare_to_copy_block(func.mlil.basic_blocks[0])
    il.set_current_address(func.start, func.arch)
    slot = Variable(func, VariableSourceType.StackVariableSourceType, 0, -0x2000)
    assert slot.type is None
    incoming = list(func.parameter_vars)[0]
    condition_size = 8 if mode == "wide_read" else 2 if mode == "low_read" else 4
    def encoded(state):
        return state | (0xA1B20000 if mode == "low_read" else 0)
    head, a, test_b, b, test_c, c = [MediumLevelILLabel() for _ in range(6)]
    il.append(il.set_var(8, slot, il.const(8, WIDE)))
    if mode in ("uniform_phi", "mixed_phi"):
        left, right = MediumLevelILLabel(), MediumLevelILLabel()
        il.append(il.if_expr(il.compare_equal(4, il.var(4, incoming), il.const(4, 0)), left, right))
        il.mark_label(left)
        il.append(il.set_var(4, slot, il.const(4, encoded(STATES[0]))))
        il.append(il.goto(head))
        il.mark_label(right)
        if mode == "uniform_phi":
            il.append(il.set_var(4, slot, il.const(4, encoded(STATES[1]))))
    else:
        il.append(il.set_var(4, slot, il.const(4, encoded(STATES[0]))))
    il.append(il.goto(head))
    il.mark_label(head)
    il.append(il.if_expr(il.compare_equal(condition_size, il.var(condition_size, slot),
                                        il.const(condition_size, STATES[0])), a, test_b))
    il.mark_label(a)
    mark = function_named(bv, "mark").start
    il.append(il.call([], il.const_pointer(bv.address_size, mark), [il.const(4, 91)]))
    if mode == "input":
        value = il.var(4, incoming)
    elif mode == "load":
        value = il.load(4, il.const_pointer(bv.address_size, bv.start))
    else:
        value = il.const(4, encoded(STATES[1]))
    if mode == "partial":
        il.append(il.set_var_field(1, slot, 0, il.const(1, STATES[1])))
    else:
        il.append(il.set_var(4, slot, value))
    il.append(il.goto(head))
    il.mark_label(test_b)
    il.append(il.if_expr(il.compare_equal(condition_size, il.var(condition_size, slot),
                                        il.const(condition_size, STATES[1])), b, test_c))
    il.mark_label(b)
    il.append(il.call([], il.const_pointer(bv.address_size, mark), [il.const(4, 92)]))
    il.append(il.set_var(4, slot, il.const(4, encoded(STATES[2]))))
    il.append(il.goto(head))
    il.mark_label(test_c)
    il.append(il.if_expr(il.compare_equal(condition_size, il.var(condition_size, slot),
                                        il.const(condition_size, STATES[2])), c, head))
    il.mark_label(c)
    il.append(il.call([], il.const_pointer(bv.address_size, mark), [il.const(4, 93)]))
    il.append(il.ret([il.const(bv.address_size, 7)]))
    il.finalize()
    il.generate_ssa_form()
    assert slot.type is None
    assert {d.size for d in il.get_var_definitions(slot)} >= {4, 8}
    return func, il, slot


def test_untyped_versions_keep_their_actual_assignment_widths(off):
    _, il, slot = _machine(off)
    graph = MLILGraph(il, view=off, ranges=[])
    definitions = [i for i in il.ssa_form.instructions
                   if i.operation == Op.MLIL_SET_VAR_SSA and i.dest.var.identifier == slot.identifier]
    assert {d.size for d in definitions} == {4, 8}
    for d in definitions:
        key = graph._key(d.dest)
        assert graph.width(key) == d.size * 8
        assert graph.definition(key) == Assign(Const(d.src.constant, d.size * 8))


@pytest.mark.parametrize("mode", ["constant", "uniform_phi", "low_read"])
def test_a_mixed_width_temporary_does_not_hide_constant_dispatcher_states(off, mode):
    func, il, slot = _machine(off, mode)
    graph = MLILGraph(il, view=off, ranges=[])
    found = plan(graph)
    assert found.threads and slot.identifier in found.states
    def marker(block):
        return next((i.params[0].constant for i in graph.il_block(block) if i.operation == Op.MLIL_CALL), None)
    transitions = {(marker(t.pred), marker(t.outcome.dest)) for t in found.threads if t.outcome is not None}
    assert {(91, 92), (92, 93)} <= transitions
    ctx = SimpleNamespace(mlil=il, function=func)
    report = unflatten.run(ctx, deadline=Deadline.after(5), ranges=[])
    assert any("edges threaded" in line for line in report), report
    assert ctx.mlil is not il
    calls = [i for i in ctx.mlil.instructions if i.operation == Op.MLIL_CALL]
    assert {i.params[0].constant for i in calls} >= {91, 92, 93}


@pytest.mark.parametrize("mode", ["mixed_phi", "input", "load", "partial", "wide_read"])
def test_ambiguous_widths_and_unknown_state_writes_stay_conservative(off, mode):
    _, il, _ = _machine(off, mode)
    graph = MLILGraph(il, view=off, ranges=[])
    assert not plan(graph).threads


def test_width_inference_stops_at_its_definition_budget(off):
    _, il, slot = _machine(off, "uniform_phi")
    graph = MLILGraph(il, view=off, ranges=[], max_nodes=1)
    phi = next(i for i in il.ssa_form.instructions
               if i.operation == Op.MLIL_VAR_PHI and i.dest.var.identifier == slot.identifier)
    key = graph._key(phi.dest)
    assert graph.width(key) == 0
    assert graph.definition(key) == Unknown()
