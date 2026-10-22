"""Per-span write proofs and bounded read-only summaries of direct callees.

An argument's type is not a read-only promise. Follow its entry SSA value through the callee's
body; storing, returning or passing the pointer to an unknown consumer still rejects it.
"""

from binaryninja import MediumLevelILOperation as Op, SSAVariable

from ...flow.uses import READ, classify
from .bounds import Bounds

_CALLS = (Op.MLIL_CALL_SSA, Op.MLIL_TAILCALL_SSA)
_CONSTANTS = (Op.MLIL_CONST, Op.MLIL_CONST_PTR)
_OPAQUE = frozenset({Op.MLIL_UNIMPL, Op.MLIL_UNIMPL_MEM, Op.MLIL_UNDEF, Op.MLIL_SYSCALL_SSA,
                     Op.MLIL_SYSCALL_UNTYPED_SSA, Op.MLIL_INTRINSIC_SSA, Op.MLIL_MEMORY_INTRINSIC_SSA,
                     Op.MLIL_CALL_UNTYPED_SSA, Op.MLIL_TAILCALL_UNTYPED_SSA, Op.MLIL_JUMP})


class Proof:
    def __init__(self, uses_of, deadline, limit=4096):
        self.uses_of, self.deadline, self.left = uses_of, deadline, limit
        self.calls, self.active = {}, set()
        self.bodies = {}

    def graph(self, uses, span=None, depth=0):
        return Graph(self, uses, span, depth)

    def argument(self, call, node, depth):
        if depth >= 4 or self.deadline.expired() or call.operation not in _CALLS \
                or call.dest.operation not in _CONSTANTS:
            return False
        caller = call.function.source_function
        if caller.get_call_type_adjustment(call.address) is not None:
            return False
        callee = caller.view.get_function_at(call.dest.constant)
        if callee is None or callee.arch != caller.arch:
            return False
        indices = [i for i, p in enumerate(call.params) if p.expr_index == node]
        if len(indices) != 1:
            return False
        index = indices[0]
        params = list(callee.parameter_vars)
        if len(params) != len(call.params) or index >= len(params):
            return False
        param = params[index]
        if param.type is None or param.type.width != caller.arch.address_size \
                or call.params[index].size != caller.arch.address_size:
            return False
        key = (callee.start, param.identifier)
        if key in self.active:
            return False
        if key in self.calls:
            return self.calls[key]
        uses = self.uses_of(callee)
        if uses is None or param in uses.ssa.aliased_vars:
            return False
        calls = self._body(uses)
        if calls is None:
            return False
        self.active.add(key)
        try:
            starts = uses.readers_of(SSAVariable(param, 0))
            graph = self.graph(uses, depth=depth + 1)
            result = (bool(starts) and classify(starts, graph)[0] == READ
                      and calls <= graph.accepted_calls)
            self.calls[key] = result
            return result
        finally:
            self.active.remove(key)

    def _body(self, uses):
        """Do not summarize opaque effects or calls omitted by the argument's use chain."""
        start = uses.ssa.source_function.start
        if start not in self.bodies:
            calls = set()
            for statement in uses.ssa.instructions:
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
    def __init__(self, proof, uses, span, depth):
        self.proof, self.uses, self.span, self.depth = proof, uses, span, depth
        self.bounds = Bounds(uses.ssa, proof.deadline) if span is not None else None
        self.stores = {}
        self.accepted_calls = set()

    def __getattr__(self, name):
        return getattr(self.uses, name)

    def kind(self, node):
        self.proof.left -= 1
        if self.proof.left < 0 or self.proof.deadline.expired():
            return "unknown"
        kind = self.uses.kind(node)
        if kind == "store" and self.bounds is not None:
            if node not in self.stores:
                self.stores[node] = self.bounds.misses(self.uses.nodes[node], *self.span)
            if self.stores[node]:
                return "store_outside"
        return kind

    def call_read_only(self, parent, node):
        if self.proof.argument(self.uses.nodes[parent], node, self.depth):
            self.accepted_calls.add(parent)
            return True
        return False
