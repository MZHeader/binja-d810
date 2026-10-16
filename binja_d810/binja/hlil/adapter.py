"""ILAdapter for HLIL (spec section 5): what the MLIL pass could not express, rewritten in HLIL.

Mutation and rollback follow the spike (docs/design/spikes/2026-10-15-v2-hlil.md): expressions
are replaced with replace_expr and backed up as shallow copies built from core_operands (the
Python API's copy_expr reads the wrong operands in 6.0.10601); dead statements are removed by
rebuilding their block, since a nop would still print; SSA is regenerated with the function's
original aliased variables.
"""

from __future__ import annotations

from typing import Hashable, Iterator

from binaryninja import (
    HighLevelILFunction, HighLevelILInstruction, HighLevelILOperation as H, ILSourceLocation, SSAVariable,
)

from ...ir.expr import Expr, is_mba
from ..adapter import Candidate, Skip
from ..deadcode import dead_definitions, dying_with_root
from ..effects import hlil_effect
from .lift import SUPPORTED_OPS, candidate_roots, children, expression_roots, lift
from .lower import lower

_ARITH = frozenset({H.HLIL_ADD, H.HLIL_SUB, H.HLIL_MUL, H.HLIL_NEG, H.HLIL_LSL})
_BITWISE = frozenset({H.HLIL_AND, H.HLIL_OR, H.HLIL_XOR, H.HLIL_NOT})
_PURE_LEAVES = frozenset({H.HLIL_VAR_SSA, H.HLIL_CONST, H.HLIL_CONST_PTR})


class HLILAdapter:
    level = "hlil"

    def __init__(self, hlil: HighLevelILFunction, max_nodes: int = 200):
        self.hlil = hlil
        self.max_nodes = max_nodes
        self.backups: list[tuple[int, int]] = []
        self.aliased_list = list(hlil.aliased_vars)
        self.aliased = frozenset(self.aliased_list)

    def has_candidates(self) -> bool:
        seen = set()
        for statement in expression_roots(self.hlil):
            stack = [statement]
            while stack:
                node = stack.pop()
                seen.add(node.operation)
                stack.extend(child for child in children(node) if child.operation not in _STRUCTURE)
        return bool(seen & _ARITH) and bool(seen & _BITWISE)

    def candidates(self, skip: Skip) -> Iterator[Candidate]:
        if self.hlil.ssa_form is None:
            self.hlil.generate_ssa_form(self.aliased_list)
        for statement in list(expression_roots(self.hlil)):
            rejected: list = []
            roots = list(candidate_roots(statement, self.aliased, rejected))
            for expr, reason in rejected:
                seen = lift(expr.ssa_form, aliased=self.aliased, max_nodes=self.max_nodes, effect_leaves=True)
                if seen is not None and is_mba(seen):
                    skip(expr.address, reason)
            for root in roots:
                inlined: dict = {}
                ir = lift(root.ssa_form, aliased=self.aliased, max_nodes=self.max_nodes, inlined=inlined)
                if ir is not None and is_mba(ir):
                    defines, single = self._defines(statement, root)
                    nodes, mixed = self._visible(root, inlined)
                    yield Candidate(root, root.address, ir, defines, single, frozenset(inlined), nodes, mixed)

    def _visible(self, root: HighLevelILInstruction, inlined: dict):
        """Size and mix of what the user sees: the root plus the definitions that die with it."""
        ssa = self.hlil.ssa_form
        definitions = {key: ssa.get_ssa_var_definition(var) for key, var in inlined.items()}
        definitions = {key: d for key, d in definitions.items() if d is not None and hasattr(d, "src")}
        uses = {key: [u.instr_index for u in ssa.get_ssa_var_uses(inlined[key])] for key in definitions}
        dying = dying_with_root(uses, {key: d.instr_index for key, d in definitions.items()},
                                root.ssa_form.instr_index)
        trees = [root] + [definitions[key].src for key in dying]
        ops = set()
        size = 0
        for tree in trees:
            for node in _expression_nodes(tree):
                ops.add(node.operation)
                size += 1
        return size, bool(ops & _ARITH) and bool(ops & _BITWISE)

    def _defines(self, statement: HighLevelILInstruction, root: HighLevelILInstruction):
        """The SSA value the root is the whole definition of, and whether it has one use."""
        if statement.operation not in (H.HLIL_VAR_INIT, H.HLIL_ASSIGN) or statement.src.expr_index != root.expr_index:
            return None, False
        dest = _defined(statement.ssa_form)
        if dest is None:
            return None, False
        return _key(dest), len(self.hlil.ssa_form.get_ssa_var_uses(dest)) == 1

    def replace(self, candidate: Candidate, replacement: Expr) -> None:
        root = candidate.root
        loc = ILSourceLocation.from_instruction(root)
        self.backups.append((root.expr_index, shallow_copy(self.hlil, root)))
        self.hlil.replace_expr(root.expr_index, lower(self.hlil, replacement, loc))

    def finish(self) -> None:
        self.hlil.finalize()
        self.hlil.generate_ssa_form(self.aliased_list)

    def dead_assignments(self) -> set[Hashable]:
        """Expression indices of pure VAR_INIT/assignment statements whose value is never used."""
        ssa = self.hlil.ssa_form
        pure, roots, statement_of = {}, [], {}
        for statement in ssa.instructions:
            dest = _defined(statement)
            if dest is not None and dest.var not in self.aliased and _pure(statement.src) \
                    and hlil_effect(statement.src, self.aliased) is None:
                key = _key(dest)
                pure[key] = [_key(v) for v in _ssa_vars(statement.src)]
                statement_of[key] = statement.non_ssa_form.expr_index
            else:
                roots.extend(_key(v) for v in _ssa_vars(statement))
        return {statement_of[key] for key in dead_definitions(pure, roots)}

    def remove(self, dead: set[Hashable]) -> None:
        blocks: dict[int, tuple[HighLevelILInstruction, set]] = {}
        for expr_index in dead:
            statement = self.hlil.get_expr(expr_index)
            parent = statement.parent if statement is not None else None
            if parent is None or parent.operation != H.HLIL_BLOCK:
                continue
            blocks.setdefault(parent.expr_index, (parent, set()))[1].add(expr_index)
        for parent, removed in blocks.values():
            body = [s.expr_index for s in parent.body if s.expr_index not in removed]
            if not body:
                continue  # never empty a block: that would change the structure
            self.backups.append((parent.expr_index, shallow_copy(self.hlil, parent)))
            self.hlil.replace_expr(parent.expr_index, self.hlil.block(body, ILSourceLocation.from_instruction(parent)))

    def cfg_shape(self) -> tuple:
        blocks = tuple((bb.index, tuple(sorted(edge.target.index for edge in bb.outgoing_edges)))
                       for bb in self.hlil.basic_blocks)
        structure = tuple(node.operation for node in _walk_structure(self.hlil.root))
        return blocks, structure

    def restore(self) -> None:
        for expr_index, backup in reversed(self.backups):
            self.hlil.replace_expr(expr_index, backup)
        if self.backups:
            self.finish()
        self.backups.clear()


_STRUCTURE = frozenset({H.HLIL_BLOCK, H.HLIL_IF, H.HLIL_WHILE, H.HLIL_DO_WHILE, H.HLIL_FOR, H.HLIL_SWITCH,
                        H.HLIL_CASE, H.HLIL_WHILE_SSA, H.HLIL_DO_WHILE_SSA, H.HLIL_FOR_SSA})
_CONTROL = _STRUCTURE | frozenset({H.HLIL_RET, H.HLIL_GOTO, H.HLIL_LABEL, H.HLIL_BREAK, H.HLIL_CONTINUE,
                                   H.HLIL_JUMP, H.HLIL_NORET, H.HLIL_UNREACHABLE, H.HLIL_TAILCALL})


def shallow_copy(hlil: HighLevelILFunction, e: HighLevelILInstruction) -> int:
    """A new expression with the same operation and operands (children shared by index).

    HighLevelILFunction.copy_expr uses raw_operands, which in 6.0.10601 returns the enclosing
    statement's decoded operands, not this expression's raw ones; core_operands has those."""
    raw = list(e.core_operands) + [0] * 5
    return hlil.expr(e.operation, raw[0], raw[1], raw[2], raw[3], raw[4], size=e.size,
                     source_location=e.source_location)


def _expression_nodes(e: HighLevelILInstruction):
    stack = [e]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in children(node) if child.operation not in _STRUCTURE)


def _walk_structure(root: HighLevelILInstruction):
    stack = [root]
    while stack:
        node = stack.pop()
        if node.operation in _CONTROL:
            yield node
        if node.operation in _STRUCTURE:
            stack.extend(reversed(list(children(node))))


def _defined(statement: HighLevelILInstruction):
    if statement.operation == H.HLIL_VAR_INIT_SSA:
        return statement.dest
    if statement.operation == H.HLIL_ASSIGN and statement.dest.operation == H.HLIL_VAR_SSA:
        return statement.dest.var
    return None


def _pure(e: HighLevelILInstruction) -> bool:
    if e.operation in _PURE_LEAVES:
        return True
    if e.operation not in SUPPORTED_OPS:
        return False
    return all(_pure(child) for child in children(e))


def _ssa_vars(e):
    """Every SSA variable in a tree. For statements that write (VAR_INIT, calls, phis) this also
    yields what they write, which only keeps more alive."""
    stack = [e]
    while stack:
        item = stack.pop()
        if isinstance(item, HighLevelILInstruction):
            stack.extend(item.operands)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, SSAVariable):
            yield item


def _key(ssa_var) -> tuple[int, int]:
    return ssa_var.var.identifier, ssa_var.version
