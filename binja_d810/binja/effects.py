"""Effect policy for candidate roots (spec section 5.2). No binaryninja import: tables use names.

An unsupported operation inside a candidate root may stay as an opaque leaf only if it is pure and
safe to evaluate again, or not at all: algebra can delete or duplicate a leaf (0 & [p] -> 0). A
supported expression containing anything else is not a candidate; its effect-free supported
sub-expressions are, so loads, calls and their order and count are never touched. A value that an
earlier instruction already materialized from a load or call is an ordinary SSA leaf.
"""

from __future__ import annotations

from typing import Optional

# Pure functions of their operands, which never trap (division may; it is not here). Anything
# not listed, including operations added to Binary Ninja later, is treated as an effect.
MLIL_PURE = frozenset({
    "MLIL_VAR_SSA", "MLIL_VAR_SSA_FIELD", "MLIL_VAR_SPLIT_SSA", "MLIL_VAR", "MLIL_VAR_FIELD", "MLIL_VAR_SPLIT",
    "MLIL_CONST", "MLIL_CONST_PTR", "MLIL_EXTERN_PTR", "MLIL_IMPORT", "MLIL_CONST_DATA", "MLIL_FLOAT_CONST",
    "MLIL_ADDRESS_OF", "MLIL_ADDRESS_OF_FIELD",
    "MLIL_ADD", "MLIL_SUB", "MLIL_MUL", "MLIL_AND", "MLIL_OR", "MLIL_XOR", "MLIL_LSL", "MLIL_LSR", "MLIL_ASR",
    "MLIL_NEG", "MLIL_NOT", "MLIL_SX", "MLIL_ZX", "MLIL_LOW_PART", "MLIL_ROL", "MLIL_ROR", "MLIL_RLC", "MLIL_RRC",
    "MLIL_ADC", "MLIL_SBB", "MLIL_MULU_DP", "MLIL_MULS_DP", "MLIL_ADD_OVERFLOW",
    "MLIL_CMP_E", "MLIL_CMP_NE", "MLIL_CMP_SLT", "MLIL_CMP_ULT", "MLIL_CMP_SLE", "MLIL_CMP_ULE",
    "MLIL_CMP_SGE", "MLIL_CMP_UGE", "MLIL_CMP_SGT", "MLIL_CMP_UGT", "MLIL_TEST_BIT", "MLIL_BOOL_TO_INT",
    "MLIL_BSWAP", "MLIL_POPCNT", "MLIL_CLZ", "MLIL_CTZ", "MLIL_RBIT", "MLIL_CLS",
    "MLIL_MINS", "MLIL_MAXS", "MLIL_MINU", "MLIL_MAXU", "MLIL_ABS",
})


def _nodes(tree):
    stack = [tree]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif hasattr(item, "operation") and hasattr(item, "operands"):
            yield item
            stack.extend(item.operands)


def mlil_effect(tree) -> Optional[str]:
    """None if every operation in the (SSA-form) tree is pure, else "effect: <operation>"."""
    for node in _nodes(tree):
        name = node.operation.name
        if name not in MLIL_PURE:
            return f"effect: {name}"
    return None
