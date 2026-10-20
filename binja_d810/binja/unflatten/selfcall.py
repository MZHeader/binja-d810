"""A call to the function itself in tail position is a back edge (v4.3, spec 3.3).

In a copy of the MLIL, a new entry jumps to the original first block, which becomes a loop head, and each self call
in tail position becomes its parameters' new values and a goto there. A self call is a call, tail call or jump to
the function's own start (a jump Binary Ninja resolved there too), or to a forwarder (a function whose MLIL only
copies its parameters and calls, tail-calls or jumps to it with them, returning the result unchanged; built
without tail calls, a stub keeps a frame): a dispatcher returning through code pointers that lead back to itself.
In tail position: a tail call or jump, or a
call whose result reaches a return through copies and width changes only (a width change the function applies to
its own result is idempotent, so applying it once instead of at every level returns the same value).

A typed call gives each parameter its argument; a call without arguments (an untyped call, a jump, a forwarder
that passes registers through) gives each parameter the variable holding its register or stack slot at the call.
The call stays when an argument is the frame's own address (spec 3.3: turning such recursion into a loop changes
which frame it names), when the counts differ, or when the new values cannot be assigned one at a time.
"""

from __future__ import annotations

from typing import Callable, Optional

from binaryninja import (ILSourceLocation, MediumLevelILFunction, MediumLevelILLabel, MediumLevelILOperation as Op,
                         VariableSourceType)

from ...unflatten.copies import sequence
from ..flow.apply import read_by

_CALLS = frozenset({Op.MLIL_CALL, Op.MLIL_CALL_UNTYPED})
_TAILS = frozenset({Op.MLIL_TAILCALL, Op.MLIL_TAILCALL_UNTYPED, Op.MLIL_JUMP, Op.MLIL_JUMP_TO})
_CONSTANTS = frozenset({Op.MLIL_CONST, Op.MLIL_CONST_PTR})
_WIDTHS = frozenset({Op.MLIL_ZX, Op.MLIL_SX, Op.MLIL_LOW_PART})
_READS = frozenset({Op.MLIL_VAR, Op.MLIL_VAR_FIELD})
_ADDRESSES = frozenset({Op.MLIL_ADDRESS_OF, Op.MLIL_ADDRESS_OF_FIELD})
_SKIP = frozenset({Op.MLIL_NOP})
MAX_TRACE = 32  # variables followed back from a call's arguments, looking for the frame's own address
MAX_STEPS = 16  # instructions followed from a call to its return
FORWARDER_BYTES = 64  # a forwarder is at most this long
MAX_COPIES = 8  # copies followed from a forwarder's argument back to its parameter

Move = tuple[object, frozenset, Callable]  # (parameter, variables the value reads, emit(new, loc) -> expression)


def loop(mlil, view) -> Optional[tuple[MediumLevelILFunction, int, int]]:
    """(the copy, the loop head's block index in it, the number of self calls turned into back edges), or None
    when the function has none it may turn."""
    func = mlil.source_function
    params = list(func.parameter_vars)
    sites: dict[int, list[Move]] = {}
    forwards: dict[int, bool] = {}
    for block in mlil.basic_blocks:
        for i in range(block.start, block.end):
            found = _site(mlil, i, func, view, params, forwards)
            if found is not None:
                sites[i] = found
                break  # the rest of the block only returns the call's result
    if not sites:
        return None
    new = MediumLevelILFunction(mlil.arch, low_level_il=mlil.llil)
    new.prepare_to_copy_function(mlil)
    head = MediumLevelILLabel()
    for block in mlil.basic_blocks:
        new.prepare_to_copy_block(block)
        if block.start == 0:  # the entry: appended before the first block is prepared, the copy has no blocks
            loc = ILSourceLocation.from_instruction(mlil[0])
            new.set_current_address(mlil[0].address, block.arch)
            new.append(new.goto(head, loc), loc)
            new.mark_label(head)
        for i in range(block.start, block.end):
            instr = mlil[i]
            new.set_current_address(instr.address, block.arch)
            loc = ILSourceLocation.from_instruction(instr)
            if i in sites:
                for dest, _, emit in sites[i]:
                    new.append(new.set_var(_size(dest), dest, emit(new, loc), loc), loc)
                new.append(new.goto(head, loc), loc)
                break
            new.append(instr.copy_to(new), loc)
    new.finalize()
    new.generate_ssa_form()
    entry = new.basic_blocks[0]  # the goto to the head, alone in its block
    return new, entry.outgoing_edges[0].target.index, len(sites)


def _site(mlil, i: int, func, view, params, forwards: dict[int, bool]) -> Optional[list[Move]]:
    """The ordered parameter moves replacing the self call at instruction `i`, or None."""
    instr = mlil[i]
    if instr.operation not in _CALLS | _TAILS or instr.dest.operation not in _CONSTANTS:
        return None
    target = instr.dest.constant
    if target not in forwards:
        forwards[target] = _reaches_self(view, target, func.start)
    if not forwards[target]:
        return None
    if instr.operation in _CALLS and not _returned(mlil, i):
        return None
    return moves(mlil, i, params)


def moves(mlil, i: int, params) -> Optional[list[Move]]:
    """The ordered moves giving `params` the arguments of the call at instruction `i`, or None: the counts
    differ, an argument is the frame's own address, or the values cannot be assigned one at a time."""
    instr = mlil[i]
    args = list(getattr(instr, "params", []))
    if len(args) == len(params) and args:
        found = [(p, frozenset(a.vars_read), _copier(a, _size(p))) for p, a in zip(params, args) if not _same(a, p)]
        seen: set = set()
        if any(_frame_address(mlil, a, seen) for a in args):
            return None
    elif not args:
        found, seen = [], set()
        for p in params:
            source = _held_at(instr, p)
            if source is None:
                return None
            if source.identifier == p.identifier:
                continue
            if _defined_from_frame(mlil, source, seen):
                return None
            found.append((p, frozenset({source}), _reader(source, _size(p))))
    else:
        return None
    # By identifier: a spliced handler's parameters are its own Variables, the same storage as the caller's.
    order = sequence([(dest.identifier, frozenset(v.identifier for v in reads)) for dest, reads, _ in found])
    return None if order is None else [found[k] for k in order]


def _reaches_self(view, target: int, start: int) -> bool:
    """`target` is the function's own start, or a forwarder to it."""
    if target == start:
        return True
    callee = view.get_function_at(target)
    if callee is None or callee.highest_address - callee.start > FORWARDER_BYTES:
        return False
    mlil = callee.mlil
    if mlil is None:
        return False
    body = [i for i in mlil.instructions if i.operation not in _SKIP]
    calls = [k for k, i in enumerate(body) if i.operation in _CALLS | _TAILS]
    if len(calls) != 1:
        return False
    call = body[calls[0]]
    if call.dest.operation not in _CONSTANTS:
        read_by(view, target, start)  # a flow fix may resolve it to the function
        return False
    if call.dest.constant != start:
        return False
    if call.operation in _CALLS and not _returned(mlil, call.instr_index, widths=False):
        return False
    if any(i.operation != Op.MLIL_SET_VAR or i.src.operation != Op.MLIL_VAR for i in body[:calls[0]]):
        return False
    forwarded = list(getattr(call, "params", []))
    own = list(callee.parameter_vars)
    return not forwarded or (len(forwarded) == len(own) and all(_copied(mlil, a, p) for a, p in zip(forwarded, own)))


def _copied(mlil, expr, param) -> bool:
    """`expr` reads `param` through copies only."""
    for _ in range(MAX_COPIES):
        if expr.operation != Op.MLIL_VAR:
            return False
        if expr.src.identifier == param.identifier:
            return True
        found = mlil.get_var_definitions(expr.src)
        if len(found) != 1 or found[0].operation != Op.MLIL_SET_VAR:
            return False
        expr = found[0].src
    return False


def _returned(mlil, i: int, widths: bool = True) -> bool:
    """The call's result reaches a return through copies and (with `widths`) width changes only, nothing else
    executed."""
    tracked = set(mlil[i].output)
    j = i + 1
    for _ in range(MAX_STEPS):
        if j >= len(mlil):
            return False
        instr = mlil[j]
        op = instr.operation
        if op in _SKIP:
            j += 1
        elif op == Op.MLIL_GOTO:
            j = instr.dest
        elif op == Op.MLIL_SET_VAR and _read_of(instr.src, widths) in tracked:
            tracked.add(instr.dest)
            j += 1
        elif op == Op.MLIL_RET:
            return all(_read_of(s, widths) in tracked for s in instr.src) and (bool(instr.src) or not mlil[i].output)
        else:
            return False
    return False


def _read_of(expr, widths: bool = True):
    """The variable `expr` reads, through width changes with `widths`, or None."""
    while widths and expr.operation in _WIDTHS:
        expr = expr.src
    if widths and expr.operation == Op.MLIL_VAR_FIELD and expr.offset == 0:
        return expr.src
    return expr.src if expr.operation == Op.MLIL_VAR else None


def _same(expr, var) -> bool:
    return expr.operation == Op.MLIL_VAR and expr.src.identifier == var.identifier


def _held_at(instr, param):
    """The variable holding the parameter's register or stack slot at `instr`, or None."""
    storage = param.storage
    try:
        if param.source_type == VariableSourceType.RegisterVariableSourceType:
            return instr.get_var_for_reg(storage)
        if param.source_type == VariableSourceType.StackVariableSourceType:
            return instr.get_var_for_stack_location(storage)
    except Exception:  # noqa: BLE001 - no variable for that storage here
        return None
    return None


def _frame_address(mlil, expr, seen: set) -> bool:
    """`expr` may be the address of something in the frame: it takes one, or reads a variable defined from one
    (every definition of it; past MAX_TRACE variables in `seen`, assumed to)."""
    stack = [expr]
    while stack:
        node = stack.pop()
        if node.operation in _ADDRESSES:
            return True
        if node.operation in _READS and _defined_from_frame(mlil, node.src, seen):
            return True
        stack.extend(o for o in node.operands if hasattr(o, "operation"))
    return False


def _defined_from_frame(mlil, var, seen: set) -> bool:
    if var in seen:
        return False  # being checked, or checked already
    seen.add(var)
    if len(seen) > MAX_TRACE:
        return True
    for d in mlil.get_var_definitions(var):
        src = getattr(d, "src", None)
        if not hasattr(src, "operation"):
            if d.operation not in _CALLS | _TAILS:  # a call's result is the callee's; anything else is unknown
                return True
            continue
        if _frame_address(mlil, src, seen):
            return True
    return False


def _size(var) -> int:
    t = var.type
    return t.width if t is not None and t.width else 8


def _copier(expr, size: int) -> Callable:
    return lambda new, loc: _fit(new, expr.copy_to(new), expr.size, size, loc)


def _reader(var, size: int) -> Callable:
    return lambda new, loc: _fit(new, new.var(_size(var), var, loc), _size(var), size, loc)


def _fit(new, expr, width: int, size: int, loc):
    """`expr`, `width` bytes wide, as the parameter's `size` bytes: its low part, or zero-extended (a register
    written at half width is zero-extended on the 64-bit architectures)."""
    if width > size:
        return new.low_part(size, expr, loc)
    if width < size:
        return new.zero_extend(size, expr, loc)
    return expr
