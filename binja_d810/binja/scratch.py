"""Proven local stack accesses and byte-range dead-store cleanup in MLIL."""

from collections import defaultdict
from dataclasses import dataclass, field
import time

from binaryninja import (
    Endianness, ILInstructionAttribute as Attr, ILSourceLocation, MediumLevelILFunction, MediumLevelILOperation as Op,
    SSAVariable, Variable, VariableSourceType,
)

from ..deadline import Deadline
from ..ir.expr import to_signed
from ..scratch.liveness import Access, analyze
from ..scratch.values import Use, unobserved
from .effects import MLIL_PURE, mlil_effect
from .mlil.adapter import MLILAdapter


@dataclass(frozen=True)
class Limits:
    instructions: int = 20000
    nodes: int = 40000
    frame_bytes: int = 4096
    visits: int = 100000
    rounds: int = 4
    address_depth: int = 64


@dataclass
class Report:
    stores: int = 0
    accesses: int = 0
    aliases: int = 0
    computations: int = 0
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    exhausted: bool = False


@dataclass(frozen=True)
class Address:
    var: object
    offset: int = 0


_READS = {Op.MLIL_VAR, Op.MLIL_VAR_FIELD, Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD,
          Op.MLIL_VAR_ALIASED, Op.MLIL_VAR_ALIASED_FIELD}
_WRITES = {Op.MLIL_SET_VAR, Op.MLIL_SET_VAR_FIELD, Op.MLIL_SET_VAR_SSA, Op.MLIL_SET_VAR_SSA_FIELD,
           Op.MLIL_SET_VAR_ALIASED, Op.MLIL_SET_VAR_ALIASED_FIELD}
_ADDRESS = {Op.MLIL_ADDRESS_OF, Op.MLIL_ADDRESS_OF_FIELD}
_LOADS = {Op.MLIL_LOAD, Op.MLIL_LOAD_STRUCT, Op.MLIL_LOAD_SSA, Op.MLIL_LOAD_STRUCT_SSA}
_STORES = {Op.MLIL_STORE, Op.MLIL_STORE_STRUCT, Op.MLIL_STORE_SSA, Op.MLIL_STORE_STRUCT_SSA}
_OPAQUE = {Op.MLIL_CALL, Op.MLIL_CALL_UNTYPED, Op.MLIL_TAILCALL, Op.MLIL_TAILCALL_UNTYPED,
           Op.MLIL_SYSCALL, Op.MLIL_SYSCALL_UNTYPED, Op.MLIL_INTRINSIC, Op.MLIL_UNIMPL,
           Op.MLIL_UNIMPL_MEM, Op.MLIL_UNDEF, Op.MLIL_JUMP}
_PROTECTED = {Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis}
_CONTROL = {Op.MLIL_NOP, Op.MLIL_IF, Op.MLIL_GOTO, Op.MLIL_JUMP_TO, Op.MLIL_RET}


def operands(node):
    for name, value, _ in node.detailed_operands:
        for item in value if isinstance(value, list) else (value,):
            yield name, item
    # Native call operands expose output variables through detailed_operands, but copy_to
    # also visits their expression wrappers (and STORE_OUTPUT addresses). Include those
    # expressions in the postorder and the effect/escape scans.
    for item in getattr(node, "output_exprs", ()):
        yield "output", item


def walk(root):
    pending = [root]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(value for _, value in operands(node) if hasattr(value, "operation"))


def var_of(value):
    return value.var if isinstance(value, SSAVariable) else value


class Scope:
    def __init__(self, il, limits, deadline):
        self.il, self.ssa, self.limits, self.deadline = il, il.ssa_form, limits, deadline
        self.nodes, self.parents, self.readers = {}, defaultdict(list), defaultdict(list)
        self.vars, self.widths, self.bytes = {}, {}, {}
        self.escaped = set()
        self.external = False
        self.barrier = False
        self.left = limits.nodes
        self.addresses, self.active = {}, set()
        self.protected = set()
        self.parameters = {v.identifier for v in il.source_function.parameter_vars}
        self.definitions = {}
        self._data_reads = None
        if len(il) > limits.instructions:
            raise ValueError("scratch instruction limit")
        for instr in il.instructions:
            for node in walk(instr):
                self.tick()
                if node.operation in _READS | _WRITES | _ADDRESS:
                    var = var_of(node.dest if node.operation in _WRITES else node.src)
                    if self.local(var):
                        self.vars[var.identifier] = var
                        size = var.type.width if var.type is not None else 0
                        accessed = 0 if node.operation in _ADDRESS else node.size + getattr(node, "offset", 0)
                        self.widths[var.identifier] = max(self.widths.get(var.identifier, 0), size, accessed)
                        if (il.arch.endianness != Endianness.LittleEndian and node.operation not in _ADDRESS
                                and (getattr(node, "offset", 0) or size and node.size != size)):
                            raise ValueError("partial stack-field layout is not modeled for this endianness")
                        if node.attributes & _PROTECTED or var.type is not None and var.type.volatile:
                            self.protected.add(var.identifier)
        offsets = set()
        for key, var in self.vars.items():
            width = self.widths[key]
            if width < 0 or width > limits.frame_bytes or var.storage + width > 0:
                raise ValueError("scratch frame bounds unavailable")
            offsets.update(range(var.storage, var.storage + width))
            if len(offsets) > limits.frame_bytes:
                raise ValueError("scratch frame byte limit")
        self.bytes = {offset: bit for bit, offset in enumerate(sorted(offsets))}
        self.all = (1 << len(self.bytes)) - 1
        for statement in self.ssa.instructions:
            pending = [(statement, None)]
            while pending:
                node, parent = pending.pop()
                if parent is not None:
                    self.parents[node.expr_index].append(parent)
                if node.expr_index in self.nodes:
                    continue
                self.tick()
                self.nodes[node.expr_index] = node
                for name, value in operands(node):
                    if hasattr(value, "operation"):
                        pending.append((value, (node, name)))
                    elif isinstance(value, SSAVariable) and name != "dest":
                        self.readers[value].append(node)
        for node in list(self.nodes.values()):
            if node.operation in _ADDRESS and self.local(node.src):
                if not self.no_escape(node):
                    self.escaped.add(node.src.identifier)
                    if self.external:
                        break

    def tick(self):
        self.left -= 1
        if self.left < 0 or self.deadline.expired():
            raise ValueError("scratch proof budget exhausted")

    def local(self, var):
        return (isinstance(var, Variable) and var.source_type == VariableSourceType.StackVariableSourceType
                and var.storage < 0 and var.identifier not in self.parameters)

    def definition(self, var):
        if var not in self.definitions:
            self.definitions[var] = self.ssa.get_ssa_var_definition(var)
        return self.definitions[var]

    def mask(self, var, offset, size):
        if var.identifier not in self.widths or size <= 0 or offset < 0 \
                or offset + size > self.widths[var.identifier]:
            return None
        result = 0
        for at in range(var.storage + offset, var.storage + offset + size):
            if at not in self.bytes:
                return None
            result |= 1 << self.bytes[at]
        return result

    def address(self, node):
        self.tick()
        key = node.expr_index
        if key in self.addresses:
            return self.addresses[key]
        if key in self.active:
            return None
        if len(self.active) >= self.limits.address_depth:
            raise ValueError("scratch address depth limit")
        self.active.add(key)
        result = None
        try:
            pointer_size = self.il.arch.address_size
            if node.operation in _ADDRESS and self.local(node.src):
                result = Address(node.src, getattr(node, "offset", 0))
            elif node.operation == Op.MLIL_VAR_SSA and node.size == pointer_size:
                if node.src.var.type is not None and node.src.var.type.volatile:
                    self.addresses[key] = None
                    return None
                definition = self.definition(node.src)
                if definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA \
                        and definition.size == pointer_size:
                    result = self.address(definition.src)
                elif definition is not None and definition.operation == Op.MLIL_VAR_PHI:
                    found = []
                    for value in definition.src:
                        d = self.definition(value)
                        found.append(self.address(d.src) if d is not None and d.operation == Op.MLIL_SET_VAR_SSA
                                     and d.size == pointer_size else None)
                    if found and found[0] is not None and all(v == found[0] for v in found):
                        result = found[0]
            elif node.operation in (Op.MLIL_ADD, Op.MLIL_SUB) and node.size == pointer_size:
                base, constant = node.left, node.right
                if node.operation == Op.MLIL_ADD and base.operation == Op.MLIL_CONST:
                    base, constant = constant, base
                if constant.operation == Op.MLIL_CONST:
                    previous = self.address(base)
                    if previous is not None:
                        delta = constant.constant if node.operation == Op.MLIL_ADD else -constant.constant
                        result = Address(previous.var, to_signed(previous.offset + delta, pointer_size * 8))
            self.addresses[key] = result
            return result
        finally:
            self.active.remove(key)

    def no_escape(self, root):
        pending, seen = [root], set()
        safe = True
        while pending:
            self.tick()
            node = pending.pop()
            if node.expr_index in seen:
                continue
            seen.add(node.expr_index)
            for parent, role in self.parents[node.expr_index]:
                op = parent.operation
                if op in _LOADS and role == "src" or op in _STORES and role == "dest":
                    address = self.address(node)
                    if address is None or self.mask(address.var, address.offset + getattr(parent, "offset", 0), parent.size) is None:
                        self.external = True
                        return False
                elif op == Op.MLIL_SET_VAR_SSA and role == "src" and parent.size == self.il.arch.address_size:
                    pending.extend(self.readers[parent.dest])
                elif op in (Op.MLIL_ADD, Op.MLIL_SUB) and self.address(parent) is not None:
                    pending.append(parent)
                elif op in (Op.MLIL_SET_VAR_ALIASED, Op.MLIL_SET_VAR_ALIASED_FIELD) and role == "src":
                    # A pointer spill that is never read can itself be eliminated first. If
                    # its data is read, the address may escape through memory and stays opaque.
                    var = parent.dest.var
                    mask = self.mask(var, getattr(parent, "offset", 0), parent.size)
                    self.external |= mask is None or bool(self.data_reads() & mask)
                    safe = False
                elif op in _STORES and role == "src":
                    address = self.address(parent.dest)
                    mask = None if address is None else self.mask(address.var,
                            address.offset + getattr(parent, "offset", 0), parent.size)
                    self.external |= mask is None or bool(self.data_reads() & mask)
                    safe = False
                else:
                    self.external = True
                    return False
            if node.operation == Op.MLIL_VAR_PHI:
                pending.extend(self.readers[node.dest])
        return safe

    def data_reads(self):
        if self._data_reads is None:
            result = 0
            for node in self.nodes.values():
                self.tick()
                mask = 0
                if node.operation in _READS and self.local(var_of(node.src)):
                    mask = self.mask(var_of(node.src), getattr(node, "offset", 0), node.size)
                elif node.operation in _LOADS:
                    address = self.address(node.src)
                    mask = None if address is None else self.mask(address.var,
                            address.offset + getattr(node, "offset", 0), node.size)
                result |= self.all if mask is None else mask
            self._data_reads = result
        return self._data_reads

    def pure(self, node):
        for part in walk(node):
            if part.attributes & _PROTECTED:
                return False
            if part.operation in _READS:
                var = var_of(part.src)
                if var.identifier in self.protected or var.type is not None and var.type.volatile:
                    return False
            if part.operation in (Op.MLIL_VAR_ALIASED, Op.MLIL_VAR_ALIASED_FIELD):
                var = part.src.var
                if var.identifier in self.escaped | self.protected or not self.local(var):
                    return False
        return mlil_effect(node, pure=lambda n: n.operation in (Op.MLIL_VAR_ALIASED, Op.MLIL_VAR_ALIASED_FIELD)) is None

    def access(self, instr):
        reads = writes = 0
        unknown = False
        for node in walk(instr):
            self.tick()
            op = node.operation
            if node.attributes & _PROTECTED or op in _OPAQUE:
                unknown = True
            if op.name not in MLIL_PURE and op not in _CONTROL | _READS | _WRITES | _ADDRESS | _LOADS | _STORES:
                unknown = True
            if op in _READS | _WRITES:
                var = var_of(node.dest if op in _WRITES else node.src)
                if not self.local(var):
                    continue
                mask = self.mask(var, getattr(node, "offset", 0), node.size)
                if mask is None:
                    unknown = True
                elif op in _READS:
                    reads |= mask
                else:
                    writes |= mask
            elif op in _LOADS | _STORES:
                pointer = node.dest if op in _STORES else node.src
                address = self.address(pointer.ssa_form)
                mask = None if address is None else self.mask(address.var,
                        address.offset + getattr(node, "offset", 0), node.size)
                if mask is None:
                    unknown = True
                elif op in _LOADS:
                    reads |= mask
                else:
                    writes |= mask
            else:
                for _, value in operands(node):
                    if self.local(var_of(value)) and op not in _ADDRESS:
                        unknown = True  # split/unsupported stack operands cannot silently disappear
        if unknown:
            reads |= self.all
            self.barrier = True
        removable = False
        if instr.operation in _WRITES and self.local(var_of(instr.dest)):
            var = var_of(instr.dest)
            removable = var.identifier not in self.escaped | self.protected and self.pure(instr.src)
        elif instr.operation in _STORES:
            address = self.address(instr.dest.ssa_form)
            removable = (address is not None and address.var.identifier not in self.escaped | self.protected
                         and self.pure(instr.src))
        return Access(reads, writes, removable and not unknown)

    def producers(self, dead):
        """Pure SSA definitions feeding the removed stores, never loads/calls materialized earlier."""
        pending = [self.il[i].ssa_form.src for i in dead]
        pending += [self.il[i].ssa_form.dest for i in dead if self.il[i].operation in _STORES]
        seen, result = set(), set()
        while pending:
            root = pending.pop()
            for node in walk(root):
                self.tick()
                if node.operation not in (Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD) or node.src in seen:
                    continue
                seen.add(node.src)
                definition = self.definition(node.src)
                if definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA \
                        and self.pure(definition.src) and not definition.attributes & _PROTECTED:
                    result.add(definition.non_ssa_form.instr_index)
                    pending.append(definition.src)
        return result

    def dead_reads(self, dead_definitions):
        if self.barrier or self.external:
            return set()
        result = set()
        for index in dead_definitions:
            instr = self.il[index]
            if instr.operation != Op.MLIL_SET_VAR or self.local(instr.dest) or not self.pure(instr.src):
                continue
            for node in walk(instr.src):
                if node.operation in _READS | _ADDRESS:
                    var = var_of(node.src)
                    if self.local(var) and var.identifier not in self.escaped | self.protected:
                        result.add(index)
                        break
        return result

    def variable_value_is_dead(self, index, dead_definitions):
        """Physical overlap alone cannot kill an older *logical* SSA value. Different native
        variables can share a storage location after propagation or a control-flow rewrite.
        Aliased writes are checked by memory liveness; plain assignments also need SSA deadness.
        """
        instruction = self.il[index].ssa_form
        if instruction.operation not in (Op.MLIL_SET_VAR_SSA, Op.MLIL_SET_VAR_SSA_FIELD):
            return True
        mask = self.mask(instruction.dest.var, getattr(instruction, "offset", 0), instruction.size)
        if mask is None:
            return False
        def users(value):
            for reader in self.readers[value]:
                if reader.operation in _READS:
                    read = self.mask(var_of(reader.src), getattr(reader, "offset", 0), reader.size)
                    yield Use(unknown=True) if read is None else Use(reads=read)
                elif reader.operation == Op.MLIL_SET_VAR_SSA_FIELD and reader.prev == value:
                    written = self.mask(reader.dest.var, reader.offset, reader.size)
                    if written is None or reader.dest.var.identifier != value.var.identifier:
                        yield Use(unknown=True)
                    else:
                        yield Use(follows=reader.dest, overwritten=written)
                elif reader.operation == Op.MLIL_VAR_PHI and reader.dest.var.identifier == value.var.identifier:
                    yield Use(follows=reader.dest)
                else:
                    yield Use(unknown=True)
        return unobserved(instruction.dest, mask, users, self.tick)

    def alias_candidates(self):
        if self.external or self.barrier:
            return {}
        # A native variable can overlap another logical value occupying the same physical
        # location. Keep those alias identities; only independent whole slots are promoted.
        overlaps, component, end = set(), [], None
        intervals = sorted((v.storage, v.storage + self.widths[k] if self.widths[k] else 0, k)
                           for k, v in self.vars.items())
        for start, stop, key in intervals:
            if end is None or start >= end:
                if len(component) > 1:
                    overlaps.update(component)
                component, end = [key], stop
            else:
                component.append(key)
                end = max(end, stop)
        if len(component) > 1:
            overlaps.update(component)
        return {key: var for key, var in self.vars.items()
                if key not in overlaps | self.escaped | self.protected and var.type is not None
                and var.type.width > 0 and var.type.width == self.widths[key]}

    def promotions(self, eligible):
        result, blocked = {}, set()
        for instr in self.il.instructions:
            for node in walk(instr):
                self.tick()
                if node.operation not in _LOADS | _STORES:
                    continue
                address = self.address((node.dest if node.operation in _STORES else node.src).ssa_form)
                if address is not None and address.var.identifier in eligible:
                    offset = address.offset + getattr(node, "offset", 0)
                    if self.mask(address.var, offset, node.size) is not None:
                        if self.il.arch.endianness != Endianness.LittleEndian and (offset or node.size != address.var.type.width):
                            blocked.add(address.var.identifier)
                            continue
                        result[node.expr_index] = Address(address.var, offset)
        for key in blocked:
            eligible.pop(key, None)
        return {index: address for index, address in result.items() if address.var.identifier in eligible}


def control_flow(il, tick):
    result = {}
    for block in il.basic_blocks:
        targets = []
        for edge in block.outgoing_edges:
            tick()
            if edge.target is None:
                raise ValueError("scratch control flow is incomplete")
            targets.append(edge.target.start)
        for index in range(block.start, block.end):
            tick()
            result[index] = (index + 1,) if index + 1 < block.end else tuple(targets)
    return result


def _copy(root, new, promotions, deadline):
    pending, copied = [(root, False)], {}
    while pending:
        if deadline.expired():
            raise ValueError("scratch commit budget exhausted")
        node, ready = pending.pop()
        if node.expr_index in copied:
            continue
        address = promotions.get(node.expr_index)
        children = [value for _, value in operands(node) if hasattr(value, "operation")]
        if address is not None:
            children = [node.src] if node.operation in _STORES else []
        if not ready:
            pending.append((node, True))
            pending.extend((child, False) for child in children)
            continue
        loc = ILSourceLocation.from_instruction(node)
        if address is None:
            result = node.copy_to(new, lambda child: copied[child.expr_index])
        else:
            whole = not address.offset and node.size == address.var.type.width
            if node.operation in _LOADS:
                result = new.var(node.size, address.var, loc) if whole else new.var_field(node.size, address.var, address.offset, loc)
            else:
                value = copied[node.src.expr_index]
                result = new.set_var(node.size, address.var, value, loc) if whole \
                    else new.set_var_field(node.size, address.var, address.offset, value, loc)
        new.set_expr_attributes(result, node.attributes)
        copied[node.expr_index] = result
    return copied[root.expr_index]


def remove_stores(il, dead, deadline, promotions=None, unaliased=()):
    new = MediumLevelILFunction(il.arch, low_level_il=il.llil)
    new.prepare_to_copy_function(il)
    for block in il.basic_blocks:
        new.prepare_to_copy_block(block)
        for instr in block:
            if deadline.expired():
                raise ValueError("scratch commit budget exhausted")
            loc = ILSourceLocation.from_instruction(instr)
            new.set_current_address(instr.address, block.arch)
            new.append(new.nop(loc) if instr.instr_index in dead else _copy(instr, new, promotions or {}, deadline), loc)
    new.finalize()
    new.generate_ssa_form(known_not_aliases=list(unaliased))
    return new


def run(il, limits=Limits(), deadline=Deadline.never()):
    started, cpu = time.monotonic(), time.thread_time()
    current, report = il, Report()
    known = {}
    try:
        for _ in range(limits.rounds):
            scope = Scope(current, limits, deadline)
            if scope.external:
                report.notes.append("frame address escapes or has unproved uses")
                break
            if not scope.bytes:
                break
            if scope.protected and "volatile or protected stack slots left unchanged" not in report.notes:
                report.notes.append("volatile or protected stack slots left unchanged")
            accesses = {i.instr_index: scope.access(i) for i in current.instructions}
            if scope.barrier and "unknown call or memory effect limits scratch cleanup" not in report.notes:
                report.notes.append("unknown call or memory effect limits scratch cleanup")
            dead_definitions = MLILAdapter(current, deadline=deadline).dead_assignments()
            dead_reads = scope.dead_reads(dead_definitions)
            if dead_reads:
                current = remove_stores(current, dead_reads, deadline, unaliased=known.values())
                report.computations += len(dead_reads)
                continue
            eligible = scope.alias_candidates()
            promotions = scope.promotions(eligible)
            known.update(eligible)
            aliased = {v.identifier for v in current.ssa_form.aliased_vars}
            dealiased = [var for key, var in eligible.items() if key in aliased]
            if promotions or dealiased:
                current = remove_stores(current, set(), deadline, promotions, known.values())
                report.aliases += len(dealiased)
                report.accesses += len(promotions)
                # Regeneration exposes the promoted values to the ordinary SSA lifter.
                # The next round recomputes every liveness fact from this new form.
                continue
            proof = analyze(accesses, control_flow(current, scope.tick), max_visits=limits.visits, deadline=deadline)
            if proof.reason:
                report.notes.append(proof.reason)
                break
            dead = {i for i in proof.dead if scope.variable_value_is_dead(i, dead_definitions)}
            if len(dead) < len(proof.dead) and "live or unmodeled SSA uses keep some stores" not in report.notes:
                report.notes.append("live or unmodeled SSA uses keep some stores")
            if not dead:
                break
            producers = scope.producers(dead)
            current = remove_stores(current, dead, deadline, unaliased=known.values())
            report.stores += len(dead)
            # These computations fed only the proven-dead stores. Keep every producer with
            # another live use and all effectful producers, even if the native adapter would
            # otherwise consider a fixed-address load disposable.
            dead_producers = MLILAdapter(current, deadline=deadline).dead_assignments() & producers
            if dead_producers:
                current = remove_stores(current, dead_producers, deadline, unaliased=known.values())
                report.computations += len(dead_producers)
        else:
            report.notes.append("scratch cleanup round limit reached")
    except (ValueError, NotImplementedError) as exc:
        report.notes.append(str(exc))
    finally:
        report.seconds, report.cpu_seconds = time.monotonic() - started, time.thread_time() - cpu
        report.exhausted = deadline.expired()
    return current, report
