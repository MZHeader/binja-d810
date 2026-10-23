"""Per-span write proofs and bounded read-only summaries of direct callees.

An argument's type is not a read-only promise. Follow its entry SSA value, returned aliases and
whole local pointer slots. Escaped storage and unknown consumers still reject the proof.
"""

from dataclasses import dataclass

from binaryninja import MediumLevelILOperation as Op, RegisterValueType, SSAVariable

from ...flow.uses import READ, classify
from .bounds import Bounds
from .slots import AddressGraph, Slots

_CALLS = (Op.MLIL_CALL_SSA, Op.MLIL_TAILCALL_SSA)
_CONSTANTS = (Op.MLIL_CONST, Op.MLIL_CONST_PTR)
_OPAQUE = frozenset({Op.MLIL_UNIMPL, Op.MLIL_UNIMPL_MEM, Op.MLIL_UNDEF, Op.MLIL_SYSCALL_SSA,
                     Op.MLIL_SYSCALL_UNTYPED_SSA, Op.MLIL_INTRINSIC_SSA, Op.MLIL_MEMORY_INTRINSIC_SSA,
                     Op.MLIL_CALL_UNTYPED_SSA, Op.MLIL_TAILCALL_UNTYPED_SSA, Op.MLIL_JUMP})


@dataclass(frozen=True)
class Summary:
    returned: bool  # a value derived from the argument may be returned; inspect each caller's use


class Proof:
    def __init__(self, uses_of, deadline, limit=4096, accepted_store=None):
        self.uses_of, self.deadline, self.left = uses_of, deadline, limit
        self.calls, self.active = {}, set()
        self.bodies = {}
        self.results = set()
        self.accepted_store = accepted_store

    def graph(self, uses, span=None, depth=0, returns=False):
        return Graph(self, uses, span, depth, returns)

    def argument(self, call, node, depth, indirect=False):
        if depth >= 4 or self.deadline.expired() or call.operation not in _CALLS \
                or call.dest.operation not in _CONSTANTS:
            return None
        caller = call.function.source_function
        if caller.get_call_type_adjustment(call.address) is not None:
            return None
        callee = caller.view.get_function_at(call.dest.constant)
        if callee is None or callee.arch != caller.arch:
            return None
        indices = [i for i, p in enumerate(call.params) if p.expr_index == node]
        if len(indices) != 1:
            return None
        index = indices[0]
        params = list(callee.parameter_vars)
        if len(params) != len(call.params) or index >= len(params):
            return None
        param = params[index]
        argument = call.params[index]
        size = caller.arch.address_size if argument.operation in (Op.MLIL_ADDRESS_OF, Op.MLIL_ADDRESS_OF_FIELD) \
            else argument.size
        if param.type is None or param.type.width != caller.arch.address_size \
                or size != caller.arch.address_size:
            return None
        key = (callee.start, param.identifier, indirect)
        if key in self.active:
            return None
        if key in self.calls:
            return self.calls[key]
        uses = self.uses_of(callee)
        if uses is None or param in uses.ssa.aliased_vars:
            return None
        calls = self._body(uses)
        if calls is None:
            return None
        self.active.add(key)
        try:
            starts = uses.readers_of(SSAVariable(param, 0))
            graph = self.graph(uses, depth=depth + 1, returns=True)
            source = AddressGraph(graph) if indirect else graph
            result = (bool(starts) and classify(starts, source)[0] == READ
                      and calls <= graph.accepted_calls)
            summary = Summary(graph.returned) if result else None
            self.calls[key] = summary
            return summary
        finally:
            self.active.remove(key)

    def _body(self, uses):
        """Do not summarize opaque effects or calls omitted by the argument's use chain."""
        start = uses.ssa.source_function.start
        if start not in self.bodies:
            calls = set()
            for statement in uses.ssa.instructions:
                uses.index(statement)
                for node in statement.traverse(lambda n: n):
                    self.left -= 1
                    if self.left < 0 or self.deadline.expired() or node.operation in _OPAQUE:
                        self.bodies[start] = None
                        return None
                    if node.operation in _CALLS:
                        calls.add(node.expr_index)
            self.bodies[start] = calls
        return self.bodies[start]


class Graph:
    def __init__(self, proof, uses, span, depth, returns=False):
        self.proof, self.uses, self.span, self.depth = proof, uses, span, depth
        self.bounds = Bounds(uses.ssa, proof.deadline) if span is not None else None
        self.stores = {}
        self.accepted_calls = set()
        self.returns, self.returned = returns, False
        self.reloads = set()
        self.slots = Slots()

    def __getattr__(self, name):
        return getattr(self.uses, name)

    def kind(self, node):
        self.proof.left -= 1
        if self.proof.left < 0 or self.proof.deadline.expired():
            return "unknown"
        if node in self.reloads:
            return "var"
        kind = self.uses.kind(node)
        if kind == "store" and self.bounds is not None:
            if node not in self.stores:
                self.stores[node] = self.bounds.misses(self.uses.nodes[node], *self.span)
            if self.stores[node]:
                return "store_outside"
            if self.proof.accepted_store is not None and self.proof.accepted_store(self.uses, self.uses.nodes[node], self.span):
                return "store_outside"
        return kind

    def call_read_only(self, parent, node):
        call = self.uses.nodes[parent]
        summary = self.proof.argument(call, node, self.depth)
        if summary is None or summary.returned and not self._result(call, node):
            return False
        self.accepted_calls.add(parent)
        return True

    def return_read_only(self, parent, node):
        if not self.returns:
            return False
        self.returned = True
        return True

    def stored_read_only(self, parent, node):
        return self.slots.store(self, self.uses.nodes[parent])

    def _result(self, call, node):
        if call.operation == Op.MLIL_TAILCALL_SSA:
            return self.return_read_only(call.expr_index, None)
        outputs = call.output
        if not outputs:
            return self._folded_result(call, node)
        key = (id(self), call.expr_index)
        if key in self.proof.results:
            return False
        self.proof.results.add(key)
        try:
            starts = [reader for var in outputs for reader in self.uses.readers_of(var)]
            return classify(starts, self)[0] == READ
        finally:
            self.proof.results.remove(key)

    def _folded_result(self, call, node):
        """Native propagation may remove a call's result definition. Follow its proven constant,
        or the unchanged entry value used as the argument, through the caller instead.
        An unknown native value is not an unused return value.
        """
        caller = self.uses.ssa.source_function
        callee = caller.view.get_function_at(call.dest.constant)
        regs = list(callee.return_regs)
        if (not regs and callee.calling_convention is not None and callee.return_type is not None
                and callee.return_type.width == caller.arch.address_size):
            reg = callee.calling_convention.int_return_reg
            if reg is not None:
                regs = [reg]
        info = caller.arch.regs.get(regs[0]) if len(regs) == 1 else None
        if info is None or info.size != caller.arch.address_size:
            return False
        value = caller.get_reg_value_after(call.address, regs[0])
        argument = self.uses.nodes[node]
        original = argument.value
        if value.type in (RegisterValueType.ConstantValue, RegisterValueType.ConstantPointerValue):
            address = value.value & ((1 << (caller.arch.address_size * 8)) - 1)
            starts = self.uses.constants_in(address, address + 1)
        elif (value.type == RegisterValueType.EntryValue and argument.operation == Op.MLIL_VAR_SSA
              and original.type == value.type and original.value == value.value
              and original.offset == value.offset == 0 and argument.size == caller.arch.address_size):
            starts = self.uses.readers_of(argument.src)
        else:
            return False
        if not starts:
            return False
        key = (id(self), call.expr_index)
        if key in self.proof.results:
            return True  # the same proven value is already being checked, including its other uses
        self.proof.results.add(key)
        try:
            return classify(starts, self)[0] == READ
        finally:
            self.proof.results.remove(key)
