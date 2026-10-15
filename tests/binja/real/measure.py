"""Structural MBA counters over a function's final HLIL (test code, independent of the plugin).

Definition (spec section 9.2): an "MBA expression in HLIL" is a maximal supported HLIL expression
whose structural lift mixes arithmetic and bitwise operations. The counter imports nothing from
binja_d810, so the same code measures frozen v1 and v2 with identical rules, and a stricter
product lifter cannot masquerade as better deobfuscation. Roots whose leaves include effectful
or otherwise unsupported operations are counted separately, as are the operations themselves.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from binaryninja import HighLevelILInstruction, HighLevelILOperation as H

SUPPORTED = frozenset({
    H.HLIL_ADD, H.HLIL_SUB, H.HLIL_MUL, H.HLIL_AND, H.HLIL_OR, H.HLIL_XOR, H.HLIL_LSL, H.HLIL_LSR,
    H.HLIL_ASR, H.HLIL_NEG, H.HLIL_NOT, H.HLIL_ZX, H.HLIL_SX, H.HLIL_LOW_PART,
})
ARITH = frozenset({H.HLIL_ADD, H.HLIL_SUB, H.HLIL_MUL, H.HLIL_NEG})
BITWISE = frozenset({H.HLIL_AND, H.HLIL_OR, H.HLIL_XOR, H.HLIL_NOT})
PLAIN_LEAVES = frozenset({
    H.HLIL_VAR, H.HLIL_VAR_SSA, H.HLIL_CONST, H.HLIL_CONST_PTR, H.HLIL_IMPORT, H.HLIL_EXTERN_PTR,
})
EFFECTS = frozenset({
    H.HLIL_DEREF, H.HLIL_DEREF_FIELD, H.HLIL_ARRAY_INDEX, H.HLIL_DEREF_SSA, H.HLIL_DEREF_FIELD_SSA,
    H.HLIL_ARRAY_INDEX_SSA, H.HLIL_CALL, H.HLIL_CALL_SSA, H.HLIL_SYSCALL, H.HLIL_SYSCALL_SSA,
    H.HLIL_TAILCALL, H.HLIL_INTRINSIC, H.HLIL_INTRINSIC_SSA, H.HLIL_DIVU, H.HLIL_DIVS, H.HLIL_MODU,
    H.HLIL_MODS, H.HLIL_DIVU_DP, H.HLIL_DIVS_DP, H.HLIL_MODU_DP, H.HLIL_MODS_DP,
})
# Structured statements: their bodies hold statements, their conditions are expressions.
STRUCTURE = frozenset({
    H.HLIL_BLOCK, H.HLIL_IF, H.HLIL_WHILE, H.HLIL_DO_WHILE, H.HLIL_FOR, H.HLIL_SWITCH, H.HLIL_CASE,
    H.HLIL_WHILE_SSA, H.HLIL_DO_WHILE_SSA, H.HLIL_FOR_SSA,
})


@dataclass
class RootCounts:
    statements: int = 0
    mixed_statements: int = 0
    roots: int = 0
    mba_roots: int = 0
    mba_roots_with_effects: int = 0
    mba_roots_with_unsupported: int = 0
    mba_nodes: int = 0
    unsupported_in_mba: Counter = field(default_factory=Counter)
    unsupported_in_mixed_statements: Counter = field(default_factory=Counter)

    def add(self, other: "RootCounts") -> None:
        for name, value in vars(other).items():
            mine = getattr(self, name)
            if isinstance(value, Counter):
                mine.update(value)
            else:
                setattr(self, name, mine + value)

    def to_json(self) -> dict:
        out = asdict(self)
        out["unsupported_in_mba"] = dict(self.unsupported_in_mba.most_common())
        out["unsupported_in_mixed_statements"] = dict(self.unsupported_in_mixed_statements.most_common())
        return out


def children(node: HighLevelILInstruction):
    for operand in node.operands:
        if isinstance(operand, HighLevelILInstruction):
            yield operand
        elif isinstance(operand, list):
            yield from (item for item in operand if isinstance(item, HighLevelILInstruction))


def subtree(node: HighLevelILInstruction):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(children(current))


def statements(hlil) -> list[HighLevelILInstruction]:
    """Leaf statements and the conditions of structured statements, each exactly once."""
    out, stack = [], [hlil.root]
    while stack:
        node = stack.pop()
        if node.operation in STRUCTURE:
            stack.extend(children(node))
        else:
            out.append(node)
    return out


def _root(root: HighLevelILInstruction, counts: RootCounts) -> None:
    ops: set = set()
    leaves: Counter = Counter()
    effect = unsupported = False
    nodes = 0
    stack = [root]
    while stack:
        node = stack.pop()
        nodes += 1
        if node.operation in SUPPORTED:
            ops.add(node.operation)
            stack.extend(children(node))
        elif node.operation not in PLAIN_LEAVES:
            leaves[node.operation.name] += 1
            if any(n.operation in EFFECTS for n in subtree(node)):
                effect = True
            else:
                unsupported = True
    counts.roots += 1
    if ops & ARITH and ops & BITWISE:
        counts.mba_roots += 1
        counts.mba_nodes += nodes
        counts.mba_roots_with_effects += effect
        counts.mba_roots_with_unsupported += unsupported and not effect
        counts.unsupported_in_mba.update(leaves)


def hlil_counts(func) -> RootCounts:
    counts = RootCounts()
    hlil = func.hlil
    if hlil is None:
        return counts
    for statement in statements(hlil):
        counts.statements += 1
        ops = Counter(n.operation for n in subtree(statement))
        ops[statement.operation] -= 1  # the statement itself (VAR_INIT, ASSIGN, RET, ...)
        if set(ops) & ARITH and set(ops) & BITWISE:
            counts.mixed_statements += 1
            for op, n in ops.items():
                if n > 0 and op not in SUPPORTED and op not in PLAIN_LEAVES:
                    counts.unsupported_in_mixed_statements[op.name] += n
        stack = [(statement, False)]
        while stack:
            node, parent_supported = stack.pop()
            supported = node.operation in SUPPORTED
            if supported and not parent_supported:
                _root(node, counts)
            stack.extend((child, supported) for child in children(node))
    return counts
