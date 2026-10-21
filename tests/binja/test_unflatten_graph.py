"""v4.2: plans over the MLIL graph (binja_d810.binja.unflatten.graph) for the flatten fixtures
(tests/samples/src/flatten.c), on 8 builds, with D-810 off."""

import pytest
from arch import IDS, MATRIX, sample
from binaryninja import ILSourceLocation, MediumLevelILFunction, MediumLevelILOperation as Op
from flowcases import has, text
from helpers import function_named, open_sample
from unflattencases import NEGATIVES, NO_JUMP_TABLE, NOT_FLATTENED, SHAPES

from binja_d810.binja.unflatten.graph import MLILGraph
from binja_d810.binja.unflatten import has_table_dispatcher, rewrite
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import Const
from binja_d810.unflatten.graph import Branch, Goto, Jump
from binja_d810.unflatten.thread import plan


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def off(request):
    bv = open_sample(sample("flatten", *request.param), d810=False)
    yield "-".join(request.param), bv
    bv.file.close()


def _plan(bv, name):
    graph = MLILGraph(function_named(bv, name).mlil, view=bv, ranges=[])
    return graph, plan(graph)


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_dispatcher_is_there_without_d810(off, name):
    key, bv = off
    hlil = text(function_named(bv, name))
    seen = any(has(hlil, hex(c)) for c in SHAPES[name])
    if (key, name) in NOT_FLATTENED:
        assert not seen, f"listed as not flattened, but a state constant is there: {(key, name)}"
        return
    assert seen, hlil


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_a_flattened_function_has_a_plan(off, name):
    key, bv = off
    if (key, name) in NOT_FLATTENED:
        return
    graph, found = _plan(bv, name)
    assert found.threads and found.states, f"{key} {name}: {found}"
    assert all(graph.name(v) for v in found.states)


def test_the_dense_switch_dispatches_through_a_jump(off):
    key, bv = off
    if (key, "fl_dense") in NOT_FLATTENED:
        return
    graph, found = _plan(bv, "fl_dense")
    jumps = any(isinstance(graph.terminator(b), Jump) for b in found.dispatcher)
    if (key, "fl_dense") in NO_JUMP_TABLE:
        assert not jumps, f"listed as having no jump table, but the dispatcher jumps: {key}"
        return
    assert jumps, f"{key}: {found.dispatcher}"


def test_the_budget_probe_recognizes_a_constant_state_jump_table(off):
    key, bv = off
    mlil = function_named(bv, "fl_dense").mlil
    found = has_table_dispatcher(mlil, bv, deadline=Deadline.after(1))
    assert found == ((key, "fl_dense") not in NO_JUMP_TABLE), key
    assert not has_table_dispatcher(mlil, bv, deadline=Deadline.after(0))


@pytest.mark.parametrize("name", NEGATIVES)
def test_the_budget_probe_ignores_other_loops(off, name):
    _, bv = off
    assert not has_table_dispatcher(function_named(bv, name).mlil, bv, deadline=Deadline.after(1))


@pytest.mark.parametrize("name", sorted(SHAPES) + NEGATIVES)
def test_a_constant_condition_is_decided(off, name):
    key, bv = off
    func = function_named(bv, name)
    graph = MLILGraph(func.mlil, view=bv, ranges=[])
    for b in graph.blocks():
        last = func.mlil[graph.il_block(b).end - 1]
        if last.operation == Op.MLIL_IF and last.condition.operation in (Op.MLIL_CONST, Op.MLIL_CONST_PTR):
            term = graph.terminator(b)
            assert isinstance(term, Branch) and term.cond == Const(1 if last.condition.constant else 0, 1), \
                f"{key} {name} block {b}: {last} -> {term}"


@pytest.mark.parametrize("name", NEGATIVES)
def test_a_negative_has_no_plan(off, name):
    key, bv = off
    graph, found = _plan(bv, name)
    assert not found.threads, f"{key} {name}: {found}"


def _gotos_as_resolved_jumps(func, mode="constant"):
    """A copy with the form BN uses after resolving an indirect jump to one internal block."""
    old = func.mlil
    new = MediumLevelILFunction(old.arch, low_level_il=old.llil)
    new.prepare_to_copy_function(old)
    for block in old.basic_blocks:
        new.prepare_to_copy_block(block)
        for instr in block:
            loc = ILSourceLocation.from_instruction(instr)
            new.set_current_address(instr.address, block.arch)
            if instr.operation == Op.MLIL_GOTO:
                address = old[instr.dest].address
                label = new.get_label_for_source_instruction(instr.dest)
                dest = new.const_pointer(old.arch.address_size, address + (mode == "different"), loc)
                if mode == "load":
                    dest = new.load(old.arch.address_size, dest, loc)
                new.append(new.jump_to(dest, {address: label}, loc), loc)
            else:
                new.append(instr.copy_to(new), loc)
    new.finalize()
    new.generate_ssa_form()
    return new


def test_a_resolved_constant_jump_can_be_threaded_like_a_goto(off):
    _, bv = off
    mlil = _gotos_as_resolved_jumps(function_named(bv, "fl_tree"))
    graph = MLILGraph(mlil, view=bv, ranges=[])
    jumps = [b for b in graph.blocks() if mlil[graph.il_block(b).end - 1].operation == Op.MLIL_JUMP_TO]
    assert jumps
    assert all(isinstance(graph.terminator(b), Goto) for b in jumps)
    found = plan(graph)
    assert any(t.pred in jumps for t in found.threads), found
    rewritten = rewrite.rewrite(graph, found)
    assert sum(i.operation == Op.MLIL_JUMP_TO for i in rewritten.instructions) < len(jumps)


@pytest.mark.parametrize("mode", ["load", "different"])
def test_a_single_target_alone_does_not_make_a_jump_unconditional(off, mode):
    _, bv = off
    mlil = _gotos_as_resolved_jumps(function_named(bv, "fl_tree"), mode)
    graph = MLILGraph(mlil, view=bv, ranges=[])
    jumps = [b for b in graph.blocks() if mlil[graph.il_block(b).end - 1].operation == Op.MLIL_JUMP_TO]
    assert jumps and all(isinstance(graph.terminator(b), Jump) for b in jumps)
