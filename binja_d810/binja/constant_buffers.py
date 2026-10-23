"""Exact bounded evaluation of a local-buffer region, retaining its exit and live values."""

from dataclasses import dataclass, field
import time

from binaryninja import (BuiltinType, Endianness, ILInstructionAttribute as Attr, ILSourceLocation,
                         MediumLevelILFunction, MediumLevelILLabel, MediumLevelILOperation as M,
                         SSAVariable, VariableSourceType)

from ..consteval.program import Address
from ..consteval.state import Cell, State, binary, unary
from ..consteval.strings import recognize
from ..deadline import Deadline
from .memory import fixed_load_is_safe
from .mlil.lift import _BINARY, _COMPARE, _UNARY
from .scratch import operands, walk


@dataclass(frozen=True)
class Limits:
    instructions: int = 4096
    nodes: int = 2000
    steps: int = 10000
    trips: int = 256
    frame_bytes: int = 4096
    work: int = 100000
    starts: int = 64
    exit_instructions: int = 4096
    scalar_bits: int = 128
    string_bytes: int = 256
    string_minimum: int = 4
    store_bytes: int = 8


@dataclass
class Report:
    regions: int = 0
    instructions: int = 0
    steps: int = 0
    strings: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    exhausted: bool = False
    evidence: tuple = ()


_PROTECTED = {Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis}
_READS = {M.MLIL_VAR, M.MLIL_VAR_FIELD}
_WRITES = {M.MLIL_SET_VAR, M.MLIL_SET_VAR_FIELD}
_LOADS = {M.MLIL_LOAD, M.MLIL_LOAD_STRUCT}
_STORES = {M.MLIL_STORE, M.MLIL_STORE_STRUCT}
_ADDRESSES = {M.MLIL_ADDRESS_OF, M.MLIL_ADDRESS_OF_FIELD}
_CALLS = {M.MLIL_CALL, M.MLIL_TAILCALL}


class Scope:
    def __init__(self, il, limits, deadline):
        self.il, self.func, self.limits, self.deadline = il, il.source_function, limits, deadline
        self.left = limits.work
        self.vars, self.extents, self.addressed = {}, {}, set()
        self.parameters = {v.identifier for v in self.func.parameter_vars}
        self.aliased = {v.identifier for v in il.ssa_form.aliased_vars}
        self.little = il.arch.endianness == Endianness.LittleEndian
        self.copies = {}
        self.readonly = {}
        self.expression_left = limits.nodes
        self.observed = set()
        self.last_writes = {}
        self.constant_writes = {}
        self.sequence = 0
        for instr in il.instructions:
            for node in walk(instr):
                self.tick()
                if node.operation in _READS | _WRITES | _ADDRESSES:
                    var = node.dest if node.operation in _WRITES else node.src
                    self.vars[var.identifier] = var
                    if self.local(var):
                        size = max(var.type.width if var.type else 0,
                                   0 if node.operation in _ADDRESSES else node.size + getattr(node, "offset", 0))
                        self.extents[var.identifier] = max(size, self.extents.get(var.identifier, 0))
                        if node.operation in _ADDRESSES:
                            self.addressed.add(var.identifier)
                elif node.operation == M.MLIL_FORCE_VER:
                    self.vars[node.dest.identifier] = node.dest
                    self.vars[node.src.identifier] = node.src
            if instr.operation == M.MLIL_CALL and (data := self.builtin(instr)) is not None:
                destination = instr.params[0]
                if destination.operation in _ADDRESSES and self.local(destination.src):
                    key = destination.src.identifier
                    self.extents[key] = max(self.extents.get(key, 0), getattr(destination, "offset", 0) + len(data))
        if not self.addressed:
            raise ValueError("no local buffer addresses")
        if any(not width or self.vars[key].storage + width > 0 for key, width in self.extents.items()):
            raise ValueError("local frame extent is unproved")
        self.base = min(self.vars[key].storage for key in self.extents)
        self.end = max(self.vars[key].storage + width for key, width in self.extents.items())
        if self.end - self.base > limits.frame_bytes:
            raise ValueError("constant frame byte limit")
        self.anchor = next(self.vars[key] for key in self.extents if self.vars[key].storage == self.base)
        self.state = State(self.end - self.base, self.little)

    def tick(self):
        self.left -= 1
        if self.left < 0 or self.deadline.expired():
            raise ValueError("constant-region proof budget exhausted")

    def local(self, var):
        return var.source_type == VariableSourceType.StackVariableSourceType and var.storage < 0 \
            and var.identifier not in self.parameters

    def protected(self, node):
        if node.attributes & _PROTECTED:
            raise ValueError(f"protected constant-region operation: {node.operation.name} {node.attributes & _PROTECTED}")
        if node.operation in _READS | _WRITES | _ADDRESSES:
            var = node.dest if node.operation in _WRITES else node.src
            if var.type is not None and var.type.volatile:
                raise ValueError("volatile constant-region variable")

    def read_var(self, var, width, offset=0):
        if self.local(var):
            if not self.little and (offset or var.type and width != 8 * var.type.width):
                raise ValueError("partial native stack fields on this endianness are unmodeled")
            cell = self.state.cells.get(var.identifier)
            if cell is not None and isinstance(cell.value, Address):
                return self.state.read(var.identifier, width, offset)
            if width % 8 or offset % 8:
                raise ValueError("non-byte local read")
            physical = self.state.load(var.storage - self.base + offset // 8, width // 8)
            if var.identifier not in self.aliased and var.identifier in self.state.cells:
                logical = self.state.read(var.identifier, width, offset)
                if logical != physical:
                    raise ValueError("logical and physical local values differ")
            return physical
        return self.state.read(var.identifier, width, offset)

    def expression(self, node):
        self.tick()
        self.expression_left -= 1
        if self.expression_left < 0:
            raise ValueError("constant expression node limit")
        self.protected(node)
        op, width = node.operation, node.size * 8 or 1
        if width > self.limits.scalar_bits:
            raise ValueError("constant scalar width limit")
        if op == M.MLIL_CONST_PTR:
            if node.constant == 0: return 0, width
            return Address("image", node.constant, width), width
        if op == M.MLIL_CONST:
            return node.constant & ((1 << width) - 1), width
        if op == M.MLIL_FLOAT_CONST:
            return int(node.raw_operands[0]), width  # raw lane bits; no floating arithmetic is evaluated
        if op in _READS:
            return self.read_var(node.src, width, 8 * getattr(node, "offset", 0)), width
        if op in _ADDRESSES:
            if not self.local(node.src):
                raise ValueError("address is not local storage")
            return Address("frame", node.src.storage - self.base + getattr(node, "offset", 0), self.il.arch.address_size * 8), self.il.arch.address_size * 8
        if op in _BINARY or op in _COMPARE:
            a, aw = self.expression(node.left); b, bw = self.expression(node.right)
            compare = op in _COMPARE
            operand_width = node.size * 8 or max(aw, bw)
            if aw != operand_width or (op not in (M.MLIL_LSL, M.MLIL_LSR, M.MLIL_ASR, M.MLIL_ROL, M.MLIL_ROR) and bw != operand_width):
                # A 1-bit comparison used as an integer has explicit 0/1 semantics in BNIL.
                if aw == 1 and not isinstance(a, Address): aw = operand_width
                if bw == 1 and not isinstance(b, Address): bw = operand_width
                if aw != operand_width or (op not in (M.MLIL_LSL, M.MLIL_LSR, M.MLIL_ASR, M.MLIL_ROL, M.MLIL_ROR) and bw != operand_width):
                    raise ValueError("constant arithmetic operand widths differ")
            return binary(_COMPARE[op] if compare else _BINARY[op], a, b, aw, bw, 1 if compare else width), 1 if compare else width
        if op in _UNARY or op == M.MLIL_BOOL_TO_INT:
            value, inner = self.expression(node.src)
            return unary("zx" if op == M.MLIL_BOOL_TO_INT else _UNARY[op], value, inner, width), width
        if op in _LOADS:
            if node.expr_type is not None and node.expr_type.volatile:
                raise ValueError("volatile constant-region load")
            address, _ = self.expression(node.src)
            offset = getattr(node, "offset", 0)
            if isinstance(address, Address) and address.buffer == "frame":
                return self.state.load(address.offset + offset, node.size), width
            if isinstance(address, Address): address = address.offset
            address += offset
            key = address, node.size
            if key not in self.readonly:
                view = self.func.view
                segment = view.get_segment_at(address)
                if not fixed_load_is_safe(view, address, node.size) or segment.writable \
                        or view.range_contains_relocation(address, node.size):
                    raise ValueError("load is not immutable image data")
                raw = view.read(address, node.size)
                if len(raw) != node.size:
                    raise ValueError("readonly bytes unavailable")
                self.readonly[key] = int.from_bytes(raw, self.state.order)
            return self.readonly[key], width
        raise ValueError(f"unsupported constant expression: {op.name}")

    def builtin(self, instr):
        if instr.expr_index in self.copies:
            return self.copies[instr.expr_index]
        found = None
        if instr.operation == M.MLIL_CALL and not instr.output and len(instr.params) in (2, 3) \
                and instr.params[1].operation == M.MLIL_CONST_DATA:
            data = instr.params[1].constant_data
            if data.size > self.limits.frame_bytes:
                raise ValueError("native constant-data byte limit")
            raw, kind = data.data_and_builtin
            raw = bytes(raw)
            if kind in (BuiltinType.BuiltinMemcpy, BuiltinType.BuiltinStrncpy) and len(instr.params) == 3 \
                    and instr.params[2].operation == M.MLIL_CONST:
                count = instr.params[2].constant
                if 0 < count <= len(raw): found = raw[:count]
            elif kind == BuiltinType.BuiltinStrcpy and len(instr.params) == 2 and b"\0" in raw:
                found = raw[:raw.index(0) + 1]
            if found is not None:
                sources = instr.llils
                if not any(n.operation.name == "LLIL_STORE_SSA" for n in sources) \
                        or any(any(word in n.operation.name for word in ("CALL", "INTRINSIC", "UNIMPL", "TRAP"))
                               or n.attributes & _PROTECTED for n in sources):
                    found = None  # Never interpret a real call, even if its argument is constant data.
        self.copies[instr.expr_index] = found
        return found

    def step(self, instr):
        self.tick(); self.protected(instr)
        self.expression_left = self.limits.nodes
        op = instr.operation
        if op in _WRITES:
            value, width = self.expression(instr.src)
            actual = instr.size * 8 or 1
            if width == 1 and actual > 1 and not isinstance(value, Address): width = actual
            if width != actual:
                raise ValueError("constant assignment width differs")
            var, offset = instr.dest, getattr(instr, "offset", 0)
            if var.source_type == VariableSourceType.StackVariableSourceType and not self.local(var):
                raise ValueError("write to incoming frame storage")
            if self.local(var):
                if not self.little and (offset or var.type and var.type.width != instr.size):
                    raise ValueError("partial native stack fields on this endianness are unmodeled")
                self.state.span(var.storage - self.base + offset, instr.size)
                if isinstance(value, Address): raise ValueError("pointer bytes are unknown")
            if op == M.MLIL_SET_VAR_FIELD:
                whole = max((var.type.width if var.type else 0) * 8, offset * 8 + actual)
                self.state.field(var.identifier, value, offset * 8, actual, whole)
            else:
                self.state.assign(var.identifier, value, actual)
            if self.local(var): self.state.store(var.storage - self.base + offset, value, instr.size)
            self.sequence += 1; self.last_writes[var.identifier] = self.sequence
            if not self.local(var):
                self.constant_writes[var.identifier, op, instr.size, offset] = (self.sequence, var, op, instr.size, offset, value)
        elif op in _STORES:
            address, _ = self.expression(instr.dest)
            value, width = self.expression(instr.src)
            if not isinstance(address, Address) or address.buffer != "frame" or width != instr.size * 8:
                raise ValueError("store is not exact local bytes")
            self.state.store(address.offset + getattr(instr, "offset", 0), value, instr.size)
        elif op == M.MLIL_FORCE_VER:
            self.state.cells[instr.dest.identifier] = self.state.cells.get(instr.src.identifier, Cell(0, (instr.dest.type.width if instr.dest.type else 0) * 8, 0))
        elif op == M.MLIL_IF:
            value, width = self.expression(instr.condition)
            if width != 1 or isinstance(value, Address): raise ValueError("unknown branch condition")
            return instr.true if value else instr.false
        elif op == M.MLIL_GOTO:
            return instr.dest
        elif op == M.MLIL_NOP:
            pass
        elif op == M.MLIL_CALL and (data := self.builtin(instr)) is not None:
            address, _ = self.expression(instr.params[0])
            if not isinstance(address, Address) or address.buffer != "frame": raise ValueError("constant copy does not target local storage")
            self.state.span(address.offset, len(data))
            for offset, value in enumerate(data): self.state.store(address.offset + offset, value, 1)
        else:
            raise ValueError(f"region boundary: {op.name}")
        return instr.instr_index + 1


def successors(il):
    result = {}
    for block in il.basic_blocks:
        for i in block:
            result[i.instr_index] = (i.instr_index + 1,) if i.instr_index + 1 < block.end else tuple(e.target.start for e in block.outgoing_edges if e.target is not None)
    return result


def reachable(edges, start, tick):
    pending, seen = [start], set()
    while pending:
        tick(); node = pending.pop()
        if node in seen: continue
        seen.add(node); pending.extend(edges.get(node, ()))
    return seen


@dataclass
class Plan:
    scope: object
    start: int
    exit: int
    visited: set
    outputs: list
    steps: int
    writes: list


def attempt(scope, start):
    il, limits = scope.il, scope.limits
    visited, visits, pc, steps = set(), {}, start, 0
    scope.state.written.clear()
    scope.last_writes.clear(); scope.constant_writes.clear(); scope.sequence = 0
    reason = ""
    while pc in range(len(il)):
        if steps >= limits.steps or scope.deadline.expired(): raise ValueError("constant evaluation step budget")
        visits[pc] = visits.get(pc, 0) + 1
        if visits[pc] > limits.trips: raise ValueError("constant evaluation trip limit")
        # A failed statement cannot partly change the candidate's memory state.
        try:
            following = scope.step(il[pc])
        except ValueError as exc:
            reason = str(exc)
            break
        visited.add(pc); steps += 1; pc = following
    if not visited or not scope.state.written or pc not in range(len(il)):
        raise ValueError(reason or "no bounded local region")
    if scope.deadline.expired() or "budget" in reason or "limit" in reason:
        raise ValueError(reason or "constant evaluation deadline")
    edges = successors(il)
    after = reachable(edges, pc, scope.tick)
    if after & visited:
        raise ValueError("retained control flow can reenter the evaluated region")
    # Only SSA values actually defined in the region and used after its exit need registers.
    outputs, seen, storage = [], set(), set()
    for index in sorted(after):
        scope.tick()
        for value in il[index].ssa_form.vars_read:
            if not isinstance(value, SSAVariable) or value in seen: continue
            seen.add(value)
            definition = il.ssa_form.get_ssa_var_definition(value)
            if definition is None or definition.non_ssa_form.instr_index not in visited: continue
            var = value.var
            if var.identifier not in scope.aliased and il[pc].get_ssa_var_version(var) != value.version:
                raise ValueError("region live-out is an older SSA version")
            cell = scope.state.cells.get(var.identifier)
            if cell is None or cell.known != (1 << cell.width) - 1:
                raise ValueError("region live-out has unknown bits")
            identity = var.source_type, var.storage
            if identity in storage:
                raise ValueError("region live-outs overlap storage")
            storage.add(identity)
            if scope.local(var):
                if isinstance(cell.value, Address) or scope.state.load(var.storage - scope.base, cell.width // 8) != cell.value:
                    raise ValueError("live logical stack value differs from physical bytes")
            outputs.append((var, cell.value, cell.width))
    # Retain the last write of each identical register/flag write shape, in execution order.
    # Keeping the original SET_VAR/FIELD widths preserves unknown high bits and private call
    # inputs; FORCE_VER is an analysis rename, not another physical register write.
    writes = sorted(scope.constant_writes.values(), key=lambda row: row[0])
    recorded = {row[1].identifier for row in writes}
    outputs = [row for row in outputs if row[0].identifier not in recorded]
    outputs.sort(key=lambda row: scope.last_writes.get(row[0].identifier, 0))
    return Plan(scope, start, pc, visited, outputs, steps, writes)


def rewrite(plan):
    scope = plan.scope; il = scope.il
    new = MediumLevelILFunction(il.arch, low_level_il=il.llil)
    new.prepare_to_copy_function(il)
    resume = MediumLevelILLabel()
    loc = ILSourceLocation.from_instruction(il[plan.start])
    def literal(value, width):
        if width <= 64: return new.const(width // 8, value, loc)
        size = width // 8
        result = new.zero_extend(size, new.const(8, value & ((1 << 64) - 1), loc), loc)
        for shift in range(64, width, 64):
            chunk = new.zero_extend(size, new.const(8, (value >> shift) & ((1 << 64) - 1), loc), loc)
            result = new.or_expr(size, result, new.shift_left(size, chunk, new.const(8, shift, loc), loc), loc)
        return result
    def pointer(value):
        if value.buffer == "image": return new.const_pointer(il.arch.address_size, value.offset, loc)
        anchors = [scope.vars[key] for key in scope.addressed if scope.vars[key].storage - scope.base <= value.offset]
        anchor = max(anchors, key=lambda v: v.storage) if anchors else scope.anchor
        offset = value.offset - (anchor.storage - scope.base)
        base = new.address_of(anchor, loc)
        return base if offset == 0 else new.add(il.arch.address_size, base, new.const(il.arch.address_size, offset, loc), loc)
    for block in il.basic_blocks:
        new.prepare_to_copy_block(block)
        for instr in block:
            scope.tick(); new.set_current_address(instr.address, block.arch)
            if instr.instr_index == plan.exit: new.mark_label(resume)
            if instr.instr_index == plan.start:
                for _, var, op, size, offset, value in plan.writes:
                    expression = pointer(value) if isinstance(value, Address) else literal(value, size * 8 or 1)
                    emitted = new.set_var_field(size, var, offset, expression, loc) if op == M.MLIL_SET_VAR_FIELD else new.set_var(size, var, expression, loc)
                    new.append(emitted, loc)
                for var, value, width in plan.outputs:
                    expression = pointer(value) if isinstance(value, Address) else literal(value, width)
                    new.append(new.set_var(width // 8, var, expression, loc), loc)
                offsets = sorted(scope.state.written); position = 0
                while position < len(offsets):
                    scope.tick(); begin = offsets[position]; end = position + 1
                    boundaries = {scope.vars[key].storage - scope.base for key in scope.addressed}
                    while end < len(offsets) and offsets[end] == offsets[end - 1] + 1 and offsets[end] not in boundaries and end - position < min(8, scope.limits.store_bytes): end += 1
                    size = end - position
                    value = scope.state.load(begin, size)
                    new.append(new.store(size, pointer(Address("frame", begin, il.arch.address_size * 8)), new.const(size, value, loc), loc), loc)
                    position = end
                new.append(new.goto(resume, loc), loc)
            if instr.instr_index not in plan.visited:
                new.append(instr.copy_to(new), ILSourceLocation.from_instruction(instr))
    new.finalize(); new.generate_ssa_form()
    return new


def strings(plan):
    scope, found = plan.scope, []
    # This read-only continuation may interpret literal synthetic copies, retaining all
    # protected original instructions. Real calls remain boundaries.
    index, visits = plan.exit, {}
    for _ in range(scope.limits.exit_instructions):
        scope.tick()
        if index not in range(len(scope.il)): break
        visits[index] = visits.get(index, 0) + 1
        if visits[index] > scope.limits.trips: break
        scope.expression_left = scope.limits.nodes
        instr = scope.il[index]
        if instr.operation == M.MLIL_CALL and (raw := scope.builtin(instr)) is not None:
            dest = instr.params[0]
            if dest.operation not in _ADDRESSES or not scope.local(dest.src) or dest.src.type and dest.src.type.volatile: break
            at = dest.src.storage - scope.base + getattr(dest, "offset", 0)
            scope.state.span(at, len(raw))
            for offset, byte in enumerate(raw): scope.state.store(at + offset, byte, 1)
            index += 1
            continue
        if instr.operation in _CALLS:
            for position, parameter in enumerate(instr.params):
                try: address, _ = scope.expression(parameter)
                except ValueError: continue
                if not isinstance(address, Address) or address.buffer != "frame": continue
                options = dict(maximum=scope.limits.string_bytes, minimum=scope.limits.string_minimum)
                text = recognize(scope.state.memory, address.offset, **options)
                if text is None and position + 1 < len(instr.params):
                    # An adjacent integer may be a length. This is a display heuristic only:
                    # no assumption about the callee or this relationship enters the proof.
                    try: length, _ = scope.expression(instr.params[position + 1])
                    except ValueError: length = None
                    if type(length) is int:
                        text = recognize(scope.state.memory, address.offset, length=length, **options)
                if text is not None: found.append((instr.address, text))
            break
        if instr.operation in _WRITES:
            try:
                value, width = scope.expression(instr.src)
                if scope.local(instr.dest) and isinstance(value, Address):
                    at = instr.dest.storage - scope.base + getattr(instr, "offset", 0)
                    scope.state.span(at, instr.size)
                    scope.state.memory[at:at + instr.size] = [None] * instr.size
                    scope.state.assign(instr.dest.identifier, value, width)
                    index += 1
                else: index = scope.step(instr)
            except ValueError: break
        else:
            try: index = scope.step(instr)
            except ValueError: break
    return found


def run(il, limits=Limits(), deadline=Deadline.never()):
    started, cpu = time.monotonic(), time.thread_time(); report = Report()
    try:
        if len(il) > limits.instructions: raise ValueError("constant instruction limit")
        first = next(iter(il.basic_blocks), None)
        if first is None or first.start != 0: return il, report
        scope = Scope(il, limits, deadline)
        prefix = State(len(scope.state.memory), scope.little)
        for start in range(min(first.end, limits.starts)):
            scope.tick()
            scope.state = State(len(prefix.memory), scope.little)
            scope.state.memory = list(prefix.memory); scope.state.cells = dict(prefix.cells)
            try:
                plan = attempt(scope, start)
                new = rewrite(plan)
                try: report.strings = strings(plan)
                except ValueError as exc: report.notes.append(f"annotation unavailable: {exc}")
                if report.strings:
                    code = [(block.start, block.end - block.start) for block in il.source_function.basic_blocks]
                    if sum(size for _, size in code) <= limits.instructions * il.arch.max_instr_length:
                        footprint = []
                        for address, size in code:
                            scope.tick()
                            raw = il.source_function.view.read(address, size)
                            if len(raw) != size: raise ValueError("comment code bytes unavailable")
                            footprint.append((address, tuple(raw)))
                        footprint += [(address, tuple(value.to_bytes(size, scope.state.order))) for (address, size), value in scope.readonly.items()]
                        report.evidence = tuple(footprint)
                    else:
                        report.notes.append("comment code-footprint limit")
                        report.strings = []
                report.regions = 1; report.instructions = len(plan.visited); report.steps = plan.steps
                report.notes.append(f"{il[start].address:#x}: evaluated {plan.steps} steps; replaced {len(plan.visited)} instructions with exact final local bytes")
                return new, report
            except (ValueError, NotImplementedError, RecursionError) as exc:
                report.strings = []; report.evidence = ()
                reason = str(exc) or "constant expression depth unsupported"
                if reason not in report.notes: report.notes.append(reason)
            # Retain an unknown pure prologue assignment; its result stays unknown.
            scope.state = prefix
            try: scope.step(il[start])
            except ValueError:
                instr = il[start]
                if instr.operation == M.MLIL_CALL and (data := scope.builtin(instr)) is not None:
                    dest = instr.params[0]
                    if dest.operation in _ADDRESSES and scope.local(dest.src) and not (dest.src.type and dest.src.type.volatile):
                        at = dest.src.storage - scope.base + getattr(dest, "offset", 0)
                        prefix.span(at, len(data))
                        for offset, byte in enumerate(data): prefix.store(at + offset, byte, 1)
                        continue  # Analyze the literal effect, while retaining the protected original.
                if instr.operation not in _WRITES: break
                prefix.cells.pop(instr.dest.identifier, None)
                if scope.local(instr.dest):
                    at = instr.dest.storage - scope.base + getattr(instr, "offset", 0)
                    prefix.span(at, instr.size); prefix.memory[at:at + instr.size] = [None] * instr.size
        return il, report
    except (ValueError, NotImplementedError, RecursionError) as exc:
        report.notes.append(str(exc) or "constant expression depth unsupported")
        return il, report
    finally:
        report.seconds, report.cpu_seconds = time.monotonic() - started, time.thread_time() - cpu
        report.exhausted = deadline.expired()
