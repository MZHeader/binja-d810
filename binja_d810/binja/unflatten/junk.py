"""Junk loops (v4.3, spec 3.7): a loop that does nothing but step its own counter until the counter meets a value.

Over the graph model (MLILGraph), a loop is junk when its body only computes values (no load, store or call),
exactly one value is carried round it (one phi at its header that the body reads), each iteration adds an odd
constant to that value, and its only exit is an equality test of the value plus a constant (or of its low bits)
against something the loop does not write. Adding an odd constant meets every value of the counter's width
within that many steps, so the loop ends; nothing it computes is read after it, and no variable it writes has
its address taken, so ending it at once changes nothing but time. In a copy of the MLIL, its header becomes a
goto to its exit. A loop is at most MAX_BLOCKS blocks of MAX_INSTRUCTIONS instructions.
"""

from __future__ import annotations

from typing import Optional

from binaryninja import ILSourceLocation, MediumLevelILFunction, MediumLevelILOperation as Op

from ...ir.expr import BinOp, Const, Expr, UnOp, Var
from ...unflatten.graph import Assign, Branch, Goto, key_of
from .graph import MLILGraph

MAX_BLOCKS = 4
MAX_INSTRUCTIONS = 32
_ADDRESSES = frozenset({Op.MLIL_ADDRESS_OF, Op.MLIL_ADDRESS_OF_FIELD})


def loops(mlil, view) -> Optional[tuple[MediumLevelILFunction, int]]:
    """(the copy with each junk loop's header turned into a goto to its exit, how many), or None."""
    back = [(e.source.index, e.target.index) for block in mlil.basic_blocks for e in block.outgoing_edges
            if e.back_edge and e.target in e.source.dominators]
    if not back:
        return None
    graph = MLILGraph(mlil, view=view, ranges=())
    exits: dict[int, int] = {}  # header block -> exit block
    for latch, header in back:
        if header in exits:
            continue
        body = _body(graph, header, latch)
        exit = None if body is None else _junk(graph, mlil, header, latch, body)
        if exit is not None:
            exits[header] = exit
    if not exits:
        return None
    new = MediumLevelILFunction(mlil.arch, low_level_il=mlil.llil)
    new.prepare_to_copy_function(mlil)
    for block in mlil.basic_blocks:
        new.prepare_to_copy_block(block)
        if block.index in exits:
            first = mlil[block.start]
            new.set_current_address(first.address, block.arch)
            loc = ILSourceLocation.from_instruction(first)
            target = new.get_label_for_source_instruction(graph.il_block(exits[block.index]).start)
            new.append(new.goto(target, loc), loc)
            continue
        for i in range(block.start, block.end):
            instr = mlil[i]
            new.set_current_address(instr.address, block.arch)
            loc = ILSourceLocation.from_instruction(instr)
            new.append(instr.copy_to(new), loc)
    new.finalize()
    new.generate_ssa_form()
    return new, len(exits)


def _body(graph: MLILGraph, header: int, latch: int) -> Optional[set[int]]:
    """The natural loop of the back edge latch -> header, or None past MAX_BLOCKS."""
    body, work = {header, latch}, [latch]
    while work:
        b = work.pop()
        if b == header:
            continue
        for e in graph.preds(b):
            if e.source not in body:
                body.add(e.source)
                work.append(e.source)
                if len(body) > MAX_BLOCKS:
                    return None
    return body


def _junk(graph: MLILGraph, mlil, header: int, latch: int, body: set[int]) -> Optional[int]:
    """The block the loop exits to, when it is junk."""
    if any(e.source not in body for b in body - {header} for e in graph.preds(b)):
        return None  # entered other than at its header
    if sum(len(graph.il_block(b)) for b in body) > MAX_INSTRUCTIONS:
        return None
    if any(step.impure for b in body for step in graph.steps(b)):
        return None
    exits, test = [], None
    for b in body:
        term = graph.terminator(b)
        if isinstance(term, Branch):
            outside = [t for t in (term.true, term.false) if t not in body]
            exits += outside
            if outside:
                test = (term, outside[0])
        elif not isinstance(term, Goto):
            return None
    if len(exits) != 1 or test is None:
        return None
    carried = [k for k in graph.phis(header) if _read_in(graph, k, body)]
    if len(carried) != 1:
        return None
    counter = carried[0]
    source = graph.phi_source(header, latch, counter)
    step = None if source is None else graph.definition(source)
    if not isinstance(step, Assign) or not _stepped(step.expr, counter, graph.width(counter)):
        return None
    branch, exit = test
    if not _meets(branch, exit, counter, graph, body):
        return None
    written = _written(graph, header, body)
    if any(_read_after(graph, key, body) for key in written):
        return None
    if _address_taken(mlil, {key.var for key in written}):
        return None
    return exit


def _read_in(graph: MLILGraph, key, body: set[int]) -> bool:
    """`key` is read by a step or test of the loop."""
    ssa_var = graph.ssa_var(key)
    for use in graph.ssa.get_ssa_var_uses(ssa_var):
        if _block_of(graph, use) in body and use.operation != Op.MLIL_VAR_PHI:
            return True
    return False


def _stepped(e: Expr, counter, width: int) -> bool:
    """`e` is the counter plus or minus an odd constant, at the counter's width."""
    if not isinstance(e, BinOp) or e.op not in ("add", "sub") or e.width != width:
        return False
    for var, const in ((e.left, e.right), (e.right, e.left)):
        if isinstance(var, Var) and key_of(var) == counter and isinstance(const, Const) and const.value & 1:
            return e.op == "add" or var is e.left
    return False


def _meets(branch: Branch, exit: int, counter, graph: MLILGraph, body: set[int]) -> bool:
    """The loop leaves when (the low bits of) the counter plus a constant equal a value it does not write."""
    cond = branch.cond
    if not isinstance(cond, BinOp) or cond.op not in ("eq", "ne"):
        return False
    if (cond.op == "eq") != (branch.true == exit):
        return False
    for side, other in ((cond.left, cond.right), (cond.right, cond.left)):
        if _affine(side, counter, graph) and _invariant(other, graph, body):
            return True
    return False


def _affine(e: Expr, counter, graph: MLILGraph, depth: int = 2) -> bool:
    """`e` is the counter plus a constant, or the low bits of one; a variable through its definition."""
    while isinstance(e, UnOp) and e.op == "trunc":
        e = e.operand
    if isinstance(e, Var):
        key = key_of(e)
        if key == counter:
            return True
        d = None if key is None or not depth else graph.definition(key)
        return isinstance(d, Assign) and _affine(d.expr, counter, graph, depth - 1)
    if isinstance(e, BinOp) and e.op in ("add", "sub"):
        for var, const in ((e.left, e.right), (e.right, e.left)):
            if isinstance(var, Var) and key_of(var) == counter and isinstance(const, Const):
                return e.op == "add" or var is e.left
    return False


def _invariant(e: Expr, graph: MLILGraph, body: set[int]) -> bool:
    if isinstance(e, Const):
        return True
    return isinstance(e, Var) and key_of(e) is not None and graph.def_block(key_of(e)) not in body


def _written(graph: MLILGraph, header: int, body: set[int]) -> set:
    found = set(graph.phis(header))
    for b in body:
        for step in graph.steps(b):
            found.update(step.writes)
    return found


def _read_after(graph: MLILGraph, key, body: set[int]) -> bool:
    """A version the loop defines is read outside it, directly or through phis."""
    work, seen = [graph.ssa_var(key)], set()
    while work:
        ssa_var = work.pop()
        if ssa_var in seen:
            continue
        seen.add(ssa_var)
        for use in graph.ssa.get_ssa_var_uses(ssa_var):
            if use.operation == Op.MLIL_VAR_PHI:
                work.append(use.dest)
            elif _block_of(graph, use) not in body:
                return True
    return False


def _block_of(graph: MLILGraph, ssa_instr) -> Optional[int]:
    return graph.block_of(ssa_instr)


def _address_taken(mlil, var_ids: set[int]) -> bool:
    def taken(node):
        if node.operation in _ADDRESSES and node.src.identifier in var_ids:
            return True
        return None
    return any(True in instr.traverse(taken) for instr in mlil.instructions)
