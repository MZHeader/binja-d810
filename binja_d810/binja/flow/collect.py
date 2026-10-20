"""Sites for the flow fixes (v4.0, spec 3.1 step 1), found in a function's MLIL at the end of the D-810 MLIL
activity: jumps and calls whose target reads tables, calls to possible identity wrappers, and (v4.3, spec 3.2)
reads of key globals that encode constants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from binaryninja import MediumLevelILOperation as Op

from ...deadline import Deadline
from ...flow.tables import Load, Read, addresses_from
from ...flow.uses import encodes, reaches_target
from ...ir.evaluate import evaluate
from ...ir.expr import BinOp, Const, Expr, Var, variables
from ...semantic.constancy import constant_value
from ...semantic.verify import Verdict
from ..mlil.lift import lift
from . import evidence

JUMPS = frozenset({Op.MLIL_JUMP, Op.MLIL_JUMP_TO})
CALLS = frozenset({Op.MLIL_CALL_SSA, Op.MLIL_TAILCALL_SSA, Op.MLIL_CALL_UNTYPED_SSA, Op.MLIL_TAILCALL_UNTYPED_SSA})
_DIRECT_CALLS = frozenset({Op.MLIL_CALL_SSA, Op.MLIL_CALL_UNTYPED_SSA})
_CONSTANTS = frozenset({Op.MLIL_CONST, Op.MLIL_CONST_PTR})
_LOADS = frozenset({Op.MLIL_LOAD_SSA, Op.MLIL_LOAD_STRUCT_SSA})
KEY_BYTES = 8  # v4.3 spec 3.2: a key is at most this wide
WRAPPER_BYTES = 64  # spec 3.1: a possible identity wrapper is at most this long
MAX_NODES = 400
MAX_MERGED = 16  # variables followed back from a merged target
SCAN_SECONDS = 2.0  # for proofs that an address is constant, per function


@dataclass(frozen=True)
class Target:
    expr: Expr  # the jump's or call's target; its leaves are the loads, named load@N
    loads: tuple[Load, ...]  # the target's loads, each followed by the key globals its index reads; none: constant
    load_exprs: frozenset[int]  # their non-SSA expression indices: pure once the target is a constant


@dataclass(frozen=True)
class Site:
    function: int
    address: int  # the jump's or call's
    kind: str  # "jump", "call", "wrapper" or "key"
    target: Optional[Target] = None
    callee: Optional[int] = None  # a wrapper's start
    trusted: bool = False  # every table const or read-only when collected (set by apply.after_pass)
    data: Optional[int] = None  # a key's address
    size: int = 0  # a key's width in bytes

    @property
    def key(self) -> tuple:
        return self.function, self.address, self.kind, self.trusted, self.data


def _is_load(leaf: Var) -> bool:
    return leaf.name.startswith("load@")


def _offset(node) -> int:
    return node.offset if node.operation == Op.MLIL_LOAD_STRUCT_SSA else 0


def _base(address: Expr) -> Optional[int]:
    """The constant an indexed address adds the index to (table + (i << 3))."""
    if isinstance(address, BinOp) and address.op == "add":
        for side in (address.left, address.right):
            if isinstance(side, Const):
                return side.value
    return None


def _possible(node, bits: int) -> Optional[tuple[int, ...]]:
    """The addresses Binary Ninja's dataflow allows a load to read (in the current MLIL), or None."""
    try:
        found = node.non_ssa_form.src.possible_values
        ranges = [(r.start, r.end, r.step) for r in (getattr(found, "ranges", None) or [])]
        values = list(getattr(found, "values", None) or [])
        return addresses_from(found.type.name, value=getattr(found, "value", 0) or 0, values=values, ranges=ranges,
                              width=bits)
    except Exception:  # noqa: BLE001 - no dataflow there
        return None


def _key_globals(address: Expr, root, read: Read, exprs: set) -> tuple[Optional[tuple[list, dict]], str]:
    """The loads inside an address (spec 3.3): each must read a constant address, a key global. Their Loads and
    values, or (None, why not)."""
    keys, env = [], {}
    for leaf in variables(address):
        if not _is_load(leaf):
            continue
        node = leaf.ref
        where = lift(node.src, root_instr=root, check_versions=False, max_nodes=MAX_NODES)
        if not isinstance(where, Const):
            return None, "an index reads memory at a computed address"
        at = where.value + _offset(node)
        value = read(at, node.size)
        if value is None:
            return None, f"cannot read {at:#x}"
        keys.append(Load(leaf.name, (at,), node.size))
        env[leaf.name] = value
        exprs.add(node.non_ssa_form.expr_index)
    return (keys, env), ""


def _loads_of(leaf: Var, root, deadline: Deadline, read: Read, exprs: set) -> tuple[Optional[list[Load]], str]:
    """The Load of one of the target's load leaves (the addresses it may read), then the key globals its index
    reads; or (None, why not)."""
    node = leaf.ref
    exprs.add(node.non_ssa_form.expr_index)
    bits = node.src.size * 8
    mask = (1 << bits) - 1
    address = lift(node.src, root_instr=root, check_versions=False, max_nodes=MAX_NODES, load_leaves=True)
    if address is None:
        return None, f"the address read at {node.address:#x} does not lift"
    offset = _offset(node)
    base = _base(address)
    if base is None and offset and isinstance(address, Const):
        base = address.value  # table[1] read as a struct field: the check starts at the table
    found, why = _key_globals(address, root, read, exprs)
    if found is None:
        return None, why
    keys, env = found
    if keys:
        if not all(_is_load(v) for v in variables(address)):
            return None, "an index reads a key global and a variable"
        return [Load(leaf.name, ((evaluate(address, env) + offset) & mask,), node.size, base), *keys], ""
    if not variables(address):
        return [Load(leaf.name, ((evaluate(address, {}) + offset) & mask,), node.size, base)], ""
    proven = constant_value(address, deadline)
    if proven is not None and proven[1].verdict is Verdict.PROVEN:
        return [Load(leaf.name, ((proven[0] + offset) & mask,), node.size, base)], ""
    values = _possible(node, bits)
    if values is None:
        return None, f"the index read at {node.address:#x} is unbounded"
    return [Load(leaf.name, tuple(sorted({(v + offset) & mask for v in values})), node.size, base)], ""


def target_of(instr, deadline: Deadline, read: Read) -> tuple[Optional[Target], str]:
    """A jump's or call's target (`instr` in SSA form) over the tables it reads, or (None, why not). A target
    merged from the same load on every path (a phi of them: tail merging) is that load."""
    found, why = _target(instr.dest, instr, deadline, read)
    if found is None and instr.dest.operation == Op.MLIL_VAR_SSA:
        merged = _merged(instr.function, instr.dest.src, deadline, read)
        if merged is not None:
            return merged, ""
    return found, why


def _merged(ssa, var, deadline: Deadline, read: Read) -> Optional[Target]:
    """The one load each source of the phi defining `var` reads, through copies and further phis, or None."""
    d = ssa.get_ssa_var_definition(var)
    if d is None or d.operation != Op.MLIL_VAR_PHI:
        return None
    targets, work, seen = [], list(d.src), {var}
    while work:
        source = work.pop()
        if source in seen:
            continue
        seen.add(source)
        if len(seen) > MAX_MERGED:
            return None
        definition = ssa.get_ssa_var_definition(source)
        if definition is not None and definition.operation == Op.MLIL_VAR_PHI:
            work.extend(definition.src)
            continue
        if definition is None or definition.operation != Op.MLIL_SET_VAR_SSA:
            return None
        if definition.src.operation == Op.MLIL_VAR_SSA:
            work.append(definition.src.src)  # a copy
            continue
        found, _ = _target(definition.src, definition, deadline, read)
        if found is None or len(found.loads) != 1 or not isinstance(found.expr, Var):
            return None
        targets.append(found)
    if not targets or len({(t.loads[0].addresses, t.loads[0].size, t.loads[0].base) for t in targets}) != 1:
        return None
    return Target(targets[0].expr, targets[0].loads, frozenset().union(*(t.load_exprs for t in targets)))


def _target(dest, instr, deadline: Deadline, read: Read) -> tuple[Optional[Target], str]:
    expr = lift(dest, root_instr=instr, check_versions=False, max_nodes=MAX_NODES, load_leaves=True)
    if expr is None:
        return None, "the target does not lift"
    leaves = variables(expr)
    if not leaves:
        # Binary Ninja read a const table itself: a constant the MLIL may say directly (no table to decode).
        return Target(expr, (), frozenset()), ""
    if not all(_is_load(v) for v in leaves):
        return None, "the target reads more than tables"
    loads, exprs = [], set()
    for leaf in leaves:
        found, why = _loads_of(leaf, instr, deadline, read, exprs)
        if found is None:
            return None, why
        loads.extend(found)
    return Target(expr, tuple(loads), frozenset(exprs)), ""


def _wrapper(view, call, uses: evidence.Uses) -> Optional[int]:
    """The start of a short callee whose result is used as a code pointer: a possible identity wrapper."""
    callee = view.get_function_at(call.dest.constant)
    if callee is None or callee.total_bytes > WRAPPER_BYTES or not call.params:
        return None
    readers = [reader for out in call.output for reader in uses.readers_of(out)]
    return callee.start if reaches_target(readers, uses) else None


def scan(mlil, deadline: Deadline, read: Read) -> Iterator[tuple[Site, object]]:
    """Each site in a function's MLIL, with its SSA instruction: an unresolved jump, a jump resolved to one target,
    or a call, whose target reads tables; and a call to a possible identity wrapper."""
    func = mlil.source_function
    ssa = mlil.ssa_form
    uses = None
    for instr in ssa.instructions:
        op = instr.operation
        if not (op == Op.MLIL_JUMP or (op == Op.MLIL_JUMP_TO and len(instr.targets) == 1) or op in CALLS):
            continue
        if instr.dest.operation in _CONSTANTS:
            if op in _DIRECT_CALLS:
                uses = uses or evidence.Uses(ssa)
                callee = _wrapper(func.view, instr, uses)
                if callee is not None:
                    yield Site(func.start, instr.address, "wrapper", callee=callee), instr
            continue
        target, _ = target_of(instr, deadline, read)
        if target is not None:
            yield Site(func.start, instr.address, "jump" if op in JUMPS else "call", target), instr


def _loads_in(instr) -> Iterator:
    stack = [instr]
    while stack:
        node = stack.pop()
        if node.operation in _LOADS:
            yield node
        for _, value, _ in node.detailed_operands:
            stack.extend(v for v in (value if isinstance(value, list) else (value,)) if hasattr(v, "operation"))


def keys(mlil) -> Iterator[Site]:
    """Each possible key a function's MLIL reads (v4.3 spec 3.2): a load of at most KEY_BYTES at a constant
    address in writable data that is not constant already, whose value encodes a constant (flow.uses.encodes),
    once per address."""
    func = mlil.source_function
    view = func.view
    ssa = mlil.ssa_form
    uses, seen = None, set()
    for instr in ssa.instructions:
        for node in _loads_in(instr):
            if node.src.operation not in _CONSTANTS or not 0 < node.size <= KEY_BYTES:
                continue
            at = node.src.constant + _offset(node)
            if at in seen or not evidence.writable_data(view, at, at + node.size):
                continue
            uses = uses or evidence.Uses(ssa)
            uses.index(instr)
            if encodes([node.expr_index], uses):
                seen.add(at)
                yield Site(func.start, node.address, "key", data=at, size=node.size)
