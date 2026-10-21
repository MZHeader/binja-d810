"""A plan's threads written into a new MLIL function (v4.2, spec §3.6).

Every original block is copied in order, each threaded edge retargeted to its trampoline, and the trampolines
appended: the non-terminator instructions of each path block, copied in order, then a goto. Blocks left
unreachable are Binary Ninja's to drop; what fed only the dispatcher is removed here (_clean). A trampoline
whose last edge is itself threaded ends in that edge's trampoline: its copy of the block is an execution of that
edge, so it may take what the edge takes, and a machine whose handlers merge before the dispatcher comes out as
one line in a single round.
"""

from __future__ import annotations

from typing import Optional

from binaryninja import ILSourceLocation, MediumLevelILFunction, MediumLevelILLabel, MediumLevelILOperation as Op

from ...ir.evaluate import evaluate
from ...ir.expr import variables
from ...unflatten.graph import Branch, Goto
from ...unflatten.thread import Outcome, Plan, Thread
from ..mlil.adapter import MLILAdapter
from ..mlil.lift import lift
from ..mlil.lower import lower
from .graph import MLILGraph

_LOADS = (Op.MLIL_LOAD_SSA, Op.MLIL_LOAD_STRUCT_SSA)


def rewrite(graph: MLILGraph, plan: Plan) -> MediumLevelILFunction:
    mlil = graph.mlil
    new = MediumLevelILFunction(mlil.arch, low_level_il=mlil.llil)
    new.prepare_to_copy_function(mlil)
    threads = [t for t in plan.threads if _emittable(graph, t)]
    labels = {(t.pred, t.slot): MediumLevelILLabel() for t in threads}
    origin: dict[int, int] = {}  # each copied instruction's index -> the index it was copied from
    for b in graph.blocks():
        block = graph.il_block(b)
        new.prepare_to_copy_block(block)
        for i in range(block.start, block.end):
            instr = mlil[i]
            new.set_current_address(instr.address, block.arch)
            loc = ILSourceLocation.from_instruction(instr)
            last = i == block.end - 1
            if last and (b, "goto") in labels and isinstance(graph.terminator(b), Goto):
                new.append(new.goto(labels[(b, "goto")], loc), loc)
            elif last and instr.operation == Op.MLIL_IF and ((b, "true") in labels or (b, "false") in labels):
                true = labels.get((b, "true")) or new.get_label_for_source_instruction(instr.true)
                false = labels.get((b, "false")) or new.get_label_for_source_instruction(instr.false)
                new.append(new.if_expr(instr.condition.copy_to(new), true, false, loc), loc)
            else:
                origin[new.append(instr.copy_to(new), loc)] = i
    in_blocks = set(origin)
    for t in threads:
        new.mark_label(labels[(t.pred, t.slot)])
        if t.split is None:
            _path(new, graph, t.outcome, labels, origin)
        else:
            _split(new, graph, t, labels, origin)
    new.finalize()
    new.generate_ssa_form()
    _clean(new, graph, plan, origin, set(origin) - in_blocks)
    return new


def _clean(new, graph: MLILGraph, plan: Plan, origin: dict[int, int], trampolines: set[int]) -> None:
    """What fed only the dispatcher dies: the state's stores (at -O0, through a stack slot) and the reads of a
    jump's table. As after the MBA pass, an assignment the rewrite left unused is removed when it is pure, and
    one dead before stays, unless it stores a constant a state variable takes (at -O0 a spare slot keeps one).
    A trampoline's load is pure when its address is constant there, in the image."""
    before = MLILAdapter(graph.mlil).dead_assignments()
    states = _state_constants(graph.mlil, plan.states)
    adapter = MLILAdapter(new)
    dead = {i for i in adapter.dead_assignments(_constant_loads(new, trampolines, adapter.fixed_loads))
            if origin.get(i) not in before or _constant(new[i]) in states}
    if dead:
        adapter.remove(dead)
        adapter.finish()


def _state_constants(mlil, states: frozenset[int]) -> frozenset[int]:
    """The constants the state variables are assigned."""
    return frozenset(c for instr in mlil.instructions
                     if instr.operation == Op.MLIL_SET_VAR and instr.dest.identifier in states
                     and (c := _constant(instr)) is not None)


def _constant(instr) -> Optional[int]:
    """The constant a SET_VAR stores, unsigned at its size."""
    if instr.operation != Op.MLIL_SET_VAR or instr.src.operation not in (Op.MLIL_CONST, Op.MLIL_CONST_PTR):
        return None
    return instr.src.constant & ((1 << 8 * instr.src.size) - 1)


def _constant_loads(new, trampolines: set[int], fixed) -> frozenset[int]:
    """Non-SSA expression indices of the loads in trampolines whose address evaluates to a constant that
    `fixed` (memory.FixedLoads) allows."""
    found, definitions = set(), {}
    for i in sorted(trampolines):
        si = new[i].ssa_form
        for node in si.traverse(lambda n: n if n.operation in _LOADS else None):
            address = lift(node.src, root_instr=si, check_versions=False, definitions=definitions)
            if address is None or variables(address):
                continue
            offset = node.offset if node.operation == Op.MLIL_LOAD_STRUCT_SSA else 0
            if fixed(evaluate(address, {}) + offset, node.size):
                found.add(node.non_ssa_form.expr_index)
    return frozenset(found)


def _emittable(graph: MLILGraph, thread: Thread) -> bool:
    """A chain tests the variable at its own size, so it needs one."""
    return thread.split is None or thread.split.condition is not None or graph.width(thread.split.key) >= 8


def _split(new, graph: MLILGraph, thread: Thread, labels: dict, origin: dict) -> None:
    last = graph.mlil[graph.il_block(thread.pred).end - 1]
    loc = ILSourceLocation.from_instruction(last)
    new.set_current_address(last.address, graph.il_block(thread.pred).arch)
    split = thread.split
    if split.condition is not None:
        one, zero = MediumLevelILLabel(), MediumLevelILLabel()
        new.append(new.if_expr(lower(new, graph.with_refs(split.condition), loc), one, zero, loc), loc)
        for label, value in ((one, split.if_one), (zero, split.if_zero)):
            new.mark_label(label)
            _path(new, graph, split.outcomes[value], labels, origin)
        return
    # The value set holds every value the variable can have here: the last one needs no test.
    size, var = graph.width(split.key) // 8, graph.ssa_var(split.key).var
    ordered = sorted(split.outcomes)
    targets = [MediumLevelILLabel() for _ in ordered]
    for value, here in zip(ordered[:-1], targets):
        rest = MediumLevelILLabel()
        test = new.compare_equal(size, new.var(size, var, loc), new.const(size, value, loc), loc)
        new.append(new.if_expr(test, here, rest, loc), loc)
        new.mark_label(rest)
    new.append(new.goto(targets[-1], loc), loc)
    for value, here in zip(ordered, targets):
        new.mark_label(here)
        _path(new, graph, split.outcomes[value], labels, origin)


def _path(new, graph: MLILGraph, outcome: Outcome, labels: dict, origin: dict) -> None:
    mlil = graph.mlil
    for b in outcome.path:
        block = graph.il_block(b)
        for i in range(block.start, block.end - 1):
            instr = mlil[i]
            new.set_current_address(instr.address, block.arch)
            loc = ILSourceLocation.from_instruction(instr)
            origin[new.append(instr.copy_to(new), loc)] = i
    end = outcome.path[-1]
    last = mlil[graph.il_block(end).end - 1]
    loc = ILSourceLocation.from_instruction(last)
    new.set_current_address(last.address, graph.il_block(end).arch)
    target = labels.get((end, _slot(graph, end, outcome.dest)))
    if target is None:
        target = new.get_label_for_source_instruction(graph.il_block(outcome.dest).start)
    new.append(new.goto(target, loc), loc)


def _slot(graph: MLILGraph, block: int, dest: int) -> str:
    term = graph.terminator(block)
    if isinstance(term, Branch):
        return "true" if term.true == dest else "false"
    return "goto"
