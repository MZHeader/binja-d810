"""Private allocation lifetimes described by semantic type attributes, never library names."""

from collections import defaultdict
from dataclasses import dataclass, field
import time

from binaryninja import (Endianness, ILInstructionAttribute as Attr, ILSourceLocation,
                         MediumLevelILFunction, MediumLevelILOperation as M, RegisterValueType,
                         SSAVariable, TypeClass, VariableSourceType)

from ..allocations.contracts import ATTRIBUTE, Contract, resolve
from ..allocations.lifetime import Event, prove
from ..deadline import Deadline
from ..ir.evaluate import evaluate
from ..ir.expr import Const, Var, substitute, to_signed, variables
from .effects import mlil_effect
from .mlil.lift import lift
from .mlil.lower import lower
from .scratch import operands, walk


@dataclass(frozen=True)
class Limits:
    instructions: int = 20000
    nodes: int = 40000
    sites: int = 64
    bytes: int = 4096
    wrapper_instructions: int = 64
    depth: int = 4
    expression_nodes: int = 2000
    contract_bytes: int = 4096


@dataclass
class Report:
    loads: int = 0
    lifetimes: int = 0
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    exhausted: bool = False


@dataclass(frozen=True)
class Binding:
    contract: Contract
    argument: object  # native argument expression, or a proven constant from a wrapper


_CALLS = {M.MLIL_CALL, M.MLIL_CALL_SSA, M.MLIL_TAILCALL, M.MLIL_TAILCALL_SSA}
_LOADS = {M.MLIL_LOAD_SSA, M.MLIL_LOAD_STRUCT_SSA}
_STORES = {M.MLIL_STORE_SSA, M.MLIL_STORE_STRUCT_SSA}
_PROTECTED = {Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis}
_CONSTANT = {RegisterValueType.ConstantValue, RegisterValueType.ConstantPointerValue, RegisterValueType.ImportedAddressValue}


class Factory:
    def __init__(self, il, limits, deadline):
        self.il, self.view, self.limits, self.deadline = il, il.source_function.view, limits, deadline
        self.left = limits.nodes
        self.active = set()

    def tick(self):
        self.left -= 1
        if self.left < 0 or self.deadline.expired():
            raise ValueError("allocation proof budget exhausted")

    def call(self, call, depth=0):
        self.tick()
        value = call.dest.value
        func = self.view.get_function_at(value.value) if value.type in _CONSTANT else None
        types = [call.dest.expr_type, func.type if func is not None else None]
        contracts = []
        if func is not None:
            declared = func.get_metadata(ATTRIBUTE)
            if declared is not None:
                contracts.append(declared)
        # Native provenance identifies the object that supplied this signature. Reading its
        # original type recovers semantic attributes lost during native signature rebuilding.
        # No catalogue of allocator or library names is used.
        addresses = {value.value} if value.type in _CONSTANT else set()
        if addresses and call.dest.operation in (M.MLIL_LOAD, M.MLIL_LOAD_SSA):
            location = call.dest.src.value
            if location.type in _CONSTANT: addresses.add(location.value)
        for address in sorted(addresses):
            self.tick()
            origin = self.view.lookup_imported_object_library(address, call.function.source_function.platform)
            if origin is not None:
                library, name = origin
                if library.arch != call.function.arch:
                    raise ValueError("allocation library architecture differs")
                types.append(library.get_named_object(name))
        for typ in types:
            if typ is not None and typ.type_class == TypeClass.PointerTypeClass:
                typ = typ.target
            if typ is not None and ATTRIBUTE in typ.attributes:
                contracts.append(typ.attributes[ATTRIBUTE])
        contract = resolve(contracts, self.limits.contract_bytes)
        if contract is not None:
            if contract.parameter >= len(call.params):
                raise ValueError("allocation contract parameter is unavailable")
            return Binding(contract, call.params[contract.parameter])
        if func is None or depth >= self.limits.depth or func.start in self.active:
            raise ValueError("semantic allocation contract unavailable")
        il = func.mlil_if_available
        if il is None or len(il) > self.limits.wrapper_instructions or len(list(il.basic_blocks)) != 1:
            raise ValueError("no bounded allocation wrapper body")
        calls = [i for i in il.instructions if i.operation in _CALLS]
        if len(calls) != 1:
            raise ValueError("wrapper has no single contracted call")
        inner = calls[0]
        for instr in il.instructions:
            self.tick()
            if instr is inner or instr.expr_index == inner.expr_index or instr.operation in (M.MLIL_RET, M.MLIL_NOP):
                continue
            if instr.operation != M.MLIL_SET_VAR or mlil_effect(instr.src.ssa_form) is not None:
                raise ValueError("wrapper has extra effects")
            if instr.dest.source_type == VariableSourceType.StackVariableSourceType and instr.dest.storage >= 0:
                raise ValueError("wrapper writes incoming frame storage")
            if any(n.operation in (M.MLIL_ADDRESS_OF, M.MLIL_ADDRESS_OF_FIELD) or n.attributes & _PROTECTED
                   for n in walk(instr)):
                raise ValueError("wrapper has protected or address-taken storage")
        self.active.add(func.start)
        try:
            bound = self.call(inner, depth + 1)
            # An ordinary call wrapper must return exactly its pointer result (or void for
            # a deallocator); a tail call already has that structural property.
            if inner.operation == M.MLIL_CALL:
                returns = [i for i in il.instructions if i.operation == M.MLIL_RET]
                if len(returns) != 1 or returns[0].instr_index <= inner.instr_index:
                    raise ValueError("wrapper return is not bounded")
                ret = returns[0]
                if bound.contract.role == "allocate":
                    if len(inner.ssa_form.output) != 1 or len(ret.src) != 1:
                        raise ValueError("wrapper pointer return is ambiguous")
                    value = lift(ret.src[0].ssa_form, root_instr=ret, check_versions=False, dataflow=False,
                                 max_nodes=self.limits.expression_nodes)
                    if not isinstance(value, Var) or value.ref != inner.ssa_form.output[0]:
                        raise ValueError("wrapper changes the allocation result")
                elif ret.src:
                    raise ValueError("deallocation wrapper returns data")
            arg = bound.argument
            if isinstance(arg, Const):
                return bound
            expression = lift(arg.ssa_form, root_instr=inner, check_versions=False, dataflow=False,
                              max_nodes=self.limits.expression_nodes)
            if expression is not None and not variables(expression):
                return Binding(bound.contract, Const(evaluate(expression, {}), expression.width))
            if isinstance(expression, Var) and isinstance(expression.ref, SSAVariable) and expression.ref.version == 0:
                parameters = list(func.parameter_vars)
                if expression.ref.var in parameters and parameters.index(expression.ref.var) < len(call.params):
                    return Binding(bound.contract, call.params[parameters.index(expression.ref.var)])
            raise ValueError("wrapper does not fix constants or forward one whole parameter")
        finally:
            self.active.remove(func.start)


class Object:
    def __init__(self, call, factory, parents, readers):
        self.call, self.factory, self.parents, self.readers = call, factory, parents, readers
        if len(call.output) != 1:
            raise ValueError("allocation outputs are ambiguous")
        self.pointer = call.output[0]
        self.aliases = {self.pointer: 0}
        self.accesses, self.release = {}, None
        self.block = call.il_basic_block

    def address(self, node):
        displacement = 0
        while node.operation in (M.MLIL_ADD, M.MLIL_SUB) and node.size == self.factory.il.arch.address_size:
            self.factory.tick()
            base, constant = node.left, node.right
            if node.operation == M.MLIL_ADD and base.operation == M.MLIL_CONST:
                base, constant = constant, base
            if constant.operation != M.MLIL_CONST or constant.size != node.size:
                return None
            displacement += constant.constant if node.operation == M.MLIL_ADD else -constant.constant
            node = base
        self.factory.tick()
        if node.operation == M.MLIL_VAR_SSA and node.size == self.factory.il.arch.address_size and node.src in self.aliases:
            return to_signed(displacement + self.aliases[node.src], 8 * self.factory.il.arch.address_size)
        return None

    def prove_uses(self):
        pending = list(self.readers.get(self.pointer, ()))
        seen = set()
        while pending:
            self.factory.tick()
            node = pending.pop()
            if node.expr_index in seen:
                continue
            seen.add(node.expr_index)
            if node.operation not in (M.MLIL_VAR_SSA, M.MLIL_ADD, M.MLIL_SUB):
                raise ValueError("pointer merge, partial use or observation is not modeled")
            if node.il_basic_block != self.block or node.instr_index <= self.call.instr_index:
                raise ValueError("allocation lifetime crosses control flow")
            if node.attributes & _PROTECTED:
                raise ValueError("protected allocation access")
            if node.operation == M.MLIL_VAR_SSA:
                typ = node.src.var.type
                if typ is not None and (typ.volatile or typ.type_class == TypeClass.PointerTypeClass and typ.target.volatile):
                    raise ValueError("volatile allocation access")
            for parent, role in self.parents[node.expr_index]:
                self.factory.tick()
                if parent.operation == M.MLIL_SET_VAR_SSA and role == "src" \
                        and parent.size == self.factory.il.arch.address_size:
                    offset = self.address(node)
                    if offset is None:
                        raise ValueError("unknown pointer copy")
                    self.aliases[parent.dest] = offset
                    pending.extend(self.readers.get(parent.dest, ()))
                elif parent.operation in (M.MLIL_ADD, M.MLIL_SUB) and self.address(parent) is not None:
                    pending.append(parent)
                elif parent.operation in _LOADS and role == "src" or parent.operation in _STORES and role == "dest":
                    offset = self.address(node)
                    if offset is None:
                        raise ValueError("unknown object address")
                    self.accesses[parent.expr_index] = (parent, offset + getattr(parent, "offset", 0))
                elif parent.operation in _CALLS and role == "params":
                    bound = self.factory.call(parent)
                    if bound.contract.role != "deallocate" or isinstance(bound.argument, Const) \
                            or bound.argument.expr_index != node.expr_index or self.address(node) != 0:
                        raise ValueError("object escapes to a call")
                    if self.release is not None and self.release[0] != parent:
                        raise ValueError("multiple releases")
                    if parent.output:
                        raise ValueError("release produces additional values")
                    self.release = parent, bound.contract
                else:
                    raise ValueError("object pointer is observed or escapes")
        if self.release is None:
            raise ValueError("matching release not proved")
        release = self.release[0]
        if release.il_basic_block != self.block:
            raise ValueError("release crosses control flow")
        for instr in self.block:
            if not self.call.instr_index < instr.instr_index < release.instr_index:
                continue
            for node in walk(instr):
                self.factory.tick()
                if node.attributes & _PROTECTED:
                    raise ValueError("protected instruction in object lifetime")
                if node.expr_index in self.accesses or node.operation in (M.MLIL_SET_VAR_SSA, M.MLIL_VAR_SSA,
                                                                          M.MLIL_NOP, M.MLIL_MEM_PHI):
                    continue
                if mlil_effect(node, pure=lambda child: child.expr_index in self.accesses) is not None:
                    raise ValueError("unknown memory or call effect within object lifetime")


def _index(il, factory):
    parents, readers, seen = defaultdict(list), defaultdict(list), set()
    pending = [(i, None) for i in il.ssa_form.instructions]
    while pending:
        factory.tick()
        node, parent = pending.pop()
        if parent is not None:
            parents[node.expr_index].append(parent)
        if node.expr_index in seen:
            continue
        seen.add(node.expr_index)
        for role, value, _ in node.detailed_operands:
            for item in value if isinstance(value, list) else (value,):
                if hasattr(item, "operation"):
                    pending.append((item, (node, role)))
                elif isinstance(item, SSAVariable) and role not in ("dest", "output"):
                    readers[item].append(node)
    return parents, readers


def _copy(node, new, replacements, deadline):
    pending, copied = [(node, False)], {}
    while pending:
        if deadline.expired():
            raise ValueError("allocation commit budget exhausted")
        current, ready = pending.pop()
        if current.expr_index in copied:
            continue
        replacement = replacements.get(current.expr_index)
        if replacement is not None:
            copied[current.expr_index] = lower(new, replacement, ILSourceLocation.from_instruction(current))
            continue
        if not ready:
            pending.append((current, True))
            pending.extend((child, False) for _, child in operands(current) if hasattr(child, "operation"))
        else:
            copied[current.expr_index] = current.copy_to(new, lambda child: copied[child.expr_index])
            new.set_expr_attributes(copied[current.expr_index], current.attributes)
    return copied[node.expr_index]


def run(il, limits=Limits(), deadline=Deadline.never()):
    started, cpu = time.monotonic(), time.thread_time()
    report, replacements, dead = Report(), {}, set()
    try:
        if len(il) > limits.instructions:
            raise ValueError("allocation instruction limit")
        factory = Factory(il, limits, deadline)
        parents = readers = None
        calls = [i for i in il.ssa_form.instructions if i.operation == M.MLIL_CALL_SSA]
        for index, call in enumerate(calls):
            if index >= limits.sites:
                report.notes.append("allocation call-site limit")
                break
            try:
                bound = factory.call(call)
                if bound.contract.role != "allocate":
                    continue
                if parents is None:
                    parents, readers = _index(il, factory)
                arg = bound.argument
                size = arg if isinstance(arg, Const) else lift(arg, root_instr=call.non_ssa_form,
                          check_versions=False, dataflow=False, max_nodes=limits.expression_nodes)
                if size is None or variables(size):
                    raise ValueError("allocation size is not constant")
                obj = Object(call, factory, parents, readers)
                obj.prove_uses()
                memory, stores = [], {}
                ordered = []
                for instruction in obj.block:
                    pending = [(instruction, False)]
                    while pending:
                        factory.tick()
                        node, ready = pending.pop()
                        if not ready:
                            pending.append((node, True))
                            pending.extend((child, False) for _, child in reversed(list(operands(node))) if hasattr(child, "operation"))
                        elif node.expr_index in obj.accesses:
                            ordered.append(obj.accesses[node.expr_index])
                for node, offset in ordered:
                    factory.tick()
                    key = node.expr_index
                    if node.operation in _STORES:
                        stores[f"allocation_store_{key}"] = node
                        memory.append(Event("store", key, offset, node.size, Var(f"allocation_store_{key}", node.size * 8)))
                    else:
                        memory.append(Event("load", key, offset, node.size))
                memory.append(Event("free", obj.release[0].expr_index))
                if any(node.instr_index >= obj.release[0].instr_index for node, _ in obj.accesses.values()):
                    raise ValueError("object accessed at or after release")
                found = prove(memory, evaluate(size, {}), bound.contract, obj.release[1], max_bytes=limits.bytes,
                              max_events=limits.nodes, little_endian=il.arch.endianness == Endianness.LittleEndian,
                              deadline=deadline)
                report.notes.extend(f"{call.address:#x}: {note}" for note in found.notes)
                staged = {}
                for key, expression in found.loads.items():
                    node = obj.accesses[key][0]
                    mapping = {}
                    for value in variables(expression):
                        factory.tick()
                        store = stores[value.name]
                        actual = lift(store.src, root_instr=node.non_ssa_form, dataflow=False,
                                      max_nodes=limits.expression_nodes)
                        if actual is None or actual.width != value.width:
                            raise ValueError("stored value is effectful or no longer available at the load")
                        mapping[value.name] = actual
                    staged[node.non_ssa_form.expr_index] = substitute(expression, mapping)
                replacements.update(staged)
                report.loads += len(staged)
                if found.elide:
                    dead.update(node.non_ssa_form.instr_index for node in (call, obj.release[0]))
                    dead.update(node.non_ssa_form.instr_index for node, _ in obj.accesses.values() if node.operation in _STORES)
                    report.lifetimes += 1
                if staged or found.elide:
                    report.notes.append(f"{call.address:#x}: proved forwarding of {len(staged)} loads from private-byte reaching stores")
            except (ValueError, NotImplementedError) as exc:
                report.notes.append(f"{call.address:#x}: {exc}")
        if not replacements and not dead:
            return il, report
        new = MediumLevelILFunction(il.arch, low_level_il=il.llil)
        new.prepare_to_copy_function(il)
        for block in il.basic_blocks:
            new.prepare_to_copy_block(block)
            for instruction in block:
                if deadline.expired():
                    raise ValueError("allocation commit budget exhausted")
                loc = ILSourceLocation.from_instruction(instruction)
                new.set_current_address(instruction.address, block.arch)
                new.append(new.nop(loc) if instruction.instr_index in dead else _copy(instruction, new, replacements, deadline), loc)
        new.finalize()
        new.generate_ssa_form()
        return new, report
    except (ValueError, NotImplementedError, RecursionError) as exc:
        report.loads = report.lifetimes = 0
        report.notes.append(f"left unchanged: {exc}")
        return il, report
    finally:
        report.seconds, report.cpu_seconds = time.monotonic() - started, time.thread_time() - cpu
        report.exhausted = deadline.expired()
