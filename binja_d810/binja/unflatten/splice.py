"""Handlers spliced into their dispatcher (v4.3b, spec 3.3): outlined flattening moves each state's code into a
function of its own, which the dispatcher calls in tail position and which returns through a call back to the
dispatcher.

In a copy of the MLIL, each call to a handler in tail position (a tail call, or a call whose result reaches a
return through copies only) becomes the handler's parameters taking the call's arguments, then the handler's own
MLIL; its calls back to the dispatcher are then self calls in tail position (selfcall). A variable keeps its
identifier, so a handler's variables may share the dispatcher's: nothing of the dispatcher is live after such a
call, and neither function reads a variable other than its parameters before writing it (a dispatcher that does
has none spliced: a handler may leave another value in that register or slot). A handler is copied, not moved:
other callers keep calling it.

A handler is a function called at a constant address with at most SPLICE_INSTRUCTIONS of MLIL and no jump table,
which ends only in returns and tail calls and calls the dispatcher (or a forwarder to it). A function with more
than MAX_HANDLERS has none spliced.
"""

from __future__ import annotations

from typing import Optional

from binaryninja import (ILSourceLocation, MediumLevelILFunction, MediumLevelILLabel, MediumLevelILOperation as Op,
                         SSAVariable)

from ..flow.apply import read_by
from . import selfcall

SPLICE_INSTRUCTIONS = 256  # MLIL instructions in a handler
MAX_HANDLERS = 64  # distinct handlers spliced into one function
_EXITS = frozenset({Op.MLIL_RET, Op.MLIL_NORET}) | selfcall._TAILS
_TARGETED = frozenset({Op.MLIL_JUMP_TO})  # instructions naming blocks other than goto and if


def handlers(mlil, view) -> Optional[tuple[MediumLevelILFunction, int]]:
    """(the copy, the number of distinct handlers spliced into it), or None when the function has none."""
    func = mlil.source_function
    found: dict[int, object] = {}
    forwards: dict[int, bool] = {}
    sites: dict[int, tuple[object, list]] = {}
    for instr in mlil.instructions:
        i = instr.instr_index
        if instr.operation not in selfcall._TAILS | selfcall._CALLS or instr.dest.operation not in selfcall._CONSTANTS:
            continue
        if instr.operation in selfcall._CALLS and not selfcall._returned(mlil, i, widths=False):
            continue
        target = instr.dest.constant
        if target not in found:
            found[target] = _handler(view, target, func, forwards)
        callee = found[target]
        if callee is None:
            continue
        moves = selfcall.moves(mlil, i, list(callee.parameter_vars))
        if moves is not None:
            sites[i] = (callee, moves)
    count = len({callee.start for callee, _ in sites.values()})
    if not sites or count > MAX_HANDLERS or _reads_entry(mlil, func.parameter_vars):
        return None
    new = MediumLevelILFunction(mlil.arch, low_level_il=mlil.llil)
    new.prepare_to_copy_function(mlil)
    for block in mlil.basic_blocks:
        new.prepare_to_copy_block(block)
        for i in range(block.start, block.end):
            instr = mlil[i]
            new.set_current_address(instr.address, block.arch)
            loc = ILSourceLocation.from_instruction(instr)
            if i not in sites:
                new.append(instr.copy_to(new), loc)
                continue
            callee, moves = sites[i]
            for dest, _, emit in moves:
                new.append(new.set_var(selfcall._size(dest), dest, emit(new, loc), loc), loc)
            _append_body(new, callee.mlil, loc)
            break  # the rest of the block only returns the call's result
    new.finalize()
    new.generate_ssa_form()
    return new, count


def _handler(view, target: int, func, forwards: dict[int, bool]):
    """The function at `target` when it is one of the dispatcher's handlers, else None."""
    if _back(view, target, func.start, forwards):
        return None  # the dispatcher itself or a forwarder to it: selfcall's
    callee = view.get_function_at(target)
    mlil = None if callee is None else callee.mlil
    if mlil is None or len(mlil) > SPLICE_INSTRUCTIONS:
        return None
    if any(not block.outgoing_edges and mlil[block.end - 1].operation not in _EXITS for block in mlil.basic_blocks):
        return None
    back = indirect = False
    for instr in mlil.instructions:
        if instr.operation in _TARGETED:
            return None
        if instr.operation in selfcall._CALLS | selfcall._TAILS:
            if instr.dest.operation not in selfcall._CONSTANTS:
                indirect = True
            else:
                back = back or _back(view, instr.dest.constant, func.start, forwards)
    if indirect:
        read_by(view, target, func.start)  # a flow fix may resolve it back to the dispatcher
    if not back or _reads_entry(mlil, callee.parameter_vars):
        return None  # an entry value of its own is the dispatcher's register or slot, not this variable's
    return callee


def _back(view, target: int, start: int, forwards: dict[int, bool]) -> bool:
    if target not in forwards:
        forwards[target] = selfcall._reaches_self(view, target, start)
    return forwards[target]


def _reads_entry(mlil, params) -> bool:
    """The function reads a variable other than its parameters before writing it: its entry version reaches a
    use other than a phi, through phis (Binary Ninja keeps phis of variables dead at the merge)."""
    own = {p.identifier for p in params}
    ssa = mlil.ssa_form
    for var in mlil.vars:
        if var.identifier in own:
            continue
        work, seen = [SSAVariable(var, 0)], set()
        while work:
            version = work.pop()
            if version in seen:
                continue
            seen.add(version)
            for use in ssa.get_ssa_var_uses(version):
                if use.operation != Op.MLIL_VAR_PHI:
                    return True
                work.append(use.dest)
    return False


def _append_body(new, mlil, loc) -> None:
    """The handler's MLIL appended where `new` is, its blocks under their own labels."""
    labels = {block.start: MediumLevelILLabel() for block in mlil.basic_blocks}
    new.append(new.goto(labels[0], loc), loc)
    for block in mlil.basic_blocks:
        new.mark_label(labels[block.start])
        for i in range(block.start, block.end):
            instr = mlil[i]
            new.set_current_address(instr.address, block.arch)
            at = ILSourceLocation.from_instruction(instr)
            if instr.operation == Op.MLIL_GOTO:
                new.append(new.goto(labels[instr.dest], at), at)
            elif instr.operation == Op.MLIL_IF:
                new.append(new.if_expr(instr.condition.copy_to(new), labels[instr.true], labels[instr.false], at), at)
            else:
                new.append(instr.copy_to(new), at)
