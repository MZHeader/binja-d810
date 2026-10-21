"""Bounded provenance checks for arguments that may name the current invocation's frame."""

from binaryninja import MediumLevelILOperation as Op, SSAVariable

from ..ssa import definition_of

_ADDRESSES = frozenset({Op.MLIL_ADDRESS_OF, Op.MLIL_ADDRESS_OF_FIELD})
_READS = frozenset({Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD})
_ALIASED = frozenset({Op.MLIL_VAR_ALIASED, Op.MLIL_VAR_ALIASED_FIELD})
_CALLS = frozenset({Op.MLIL_CALL_SSA, Op.MLIL_TAILCALL_SSA,
                   Op.MLIL_CALL_UNTYPED_SSA, Op.MLIL_TAILCALL_UNTYPED_SSA})
_CONSTANTS = frozenset({Op.MLIL_CONST, Op.MLIL_CONST_PTR})
_LOADS = frozenset({Op.MLIL_LOAD_SSA, Op.MLIL_LOAD_STRUCT_SSA})


class Arguments:
    """Follow each argument's reaching SSA definitions. Proven independent values are reusable;
    incomplete or ambiguous proofs stay conservative. The bound counts definitions that need expansion.
    """

    def __init__(self, mlil, limit: int):
        self.ssa, self.limit = mlil.ssa_form, limit
        self.definitions, self.safe = {}, set()
        self._takes_address = None

    def _frame_address_exists(self) -> bool:
        """Without a memory-alias proof, an exposed frame pointer may have been stored and reloaded,
        or recovered by a helper. Cache this scan for the whole function, not once per argument."""
        if self._takes_address is None:
            self._takes_address = any(
                any(i.traverse(lambda n: True if n.operation in _ADDRESSES else None))
                for i in self.ssa.instructions)
        return self._takes_address

    def may_name_frame(self, root) -> bool:
        work, seen, expressions = [root], set(), set()
        expanded = 0
        while work:
            node = work.pop()
            if isinstance(node, SSAVariable):
                if node in seen or node in self.safe:
                    continue
                seen.add(node)
                definition = definition_of(self.ssa, node, self.definitions)
                if definition is None:
                    if node.version != 0:
                        return True
                    continue  # an incoming value was not constructed from this invocation's frame
                op = definition.operation
                if op == Op.MLIL_SET_VAR_SSA and definition.src.operation in _CONSTANTS:
                    continue
                expanded += 1
                if expanded > self.limit:
                    return True
                if op == Op.MLIL_VAR_PHI:
                    work.extend(definition.src)
                elif op == Op.MLIL_SET_VAR_SSA:
                    work.append(definition.src)
                elif op == Op.MLIL_SET_VAR_SSA_FIELD:
                    work.extend((definition.prev, definition.src))
                elif op in _CALLS:
                    if self._frame_address_exists():
                        return True
                    # A helper may return an alias of one of its arguments.
                    work.extend(definition.params)
                else:
                    return True
                continue
            if node.expr_index in expressions:
                continue
            expressions.add(node.expr_index)
            op = node.operation
            if op in _ADDRESSES or op in _ALIASED or op.name.startswith(("MLIL_UNDEF", "MLIL_UNIMPL")):
                return True
            if op in _LOADS and self._frame_address_exists():
                return True
            if op in _READS:
                work.append(node.src)
                continue
            for operand in node.operands:
                for child in operand if isinstance(operand, list) else (operand,):
                    if isinstance(child, SSAVariable) or hasattr(child, "operation"):
                        work.append(child)
        self.safe.update(seen)
        return False
