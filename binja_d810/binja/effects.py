"""Effect policy for candidate roots. No binaryninja import: tables use names.

An unsupported operation inside a candidate root may stay as an opaque leaf only if it is pure and
safe to evaluate again, or not at all: algebra can delete or duplicate a leaf (0 & [p] -> 0). A
supported expression containing anything else is not a candidate; its effect-free supported
sub-expressions are, so loads, calls and their order and count are never touched. A value that an
earlier instruction already materialized from a load or call is an ordinary SSA leaf.
"""

from __future__ import annotations

from typing import Callable, Optional

# Pure functions of their operands, which never trap (division may: it is pure only once proven never
# to trap, which the lifter decides, spec v3.2). Anything
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


def mlil_effect(tree, pure: Optional[Callable] = None) -> Optional[str]:
    """None if every operation in the (SSA-form) tree is pure, else "effect: <operation>". `pure(node)`
    may accept a node outside the table: a division proven never to trap."""
    for node in _nodes(tree):
        name = node.operation.name
        if name not in MLIL_PURE and (pure is None or not pure(node)):
            return f"effect: {name}"
    return None


# HLIL: a candidate's leaves are variables, constants and field reads of variables; supported
# operations combine them. Everything else bounds a candidate. These are the operations that may
# stay *inside* an HLIL expression the pass leaves alone without making it unsafe to rewrite the
# pure parts around them; any other operation found under a supported node splits it.
HLIL_LEAVES = frozenset({"HLIL_VAR", "HLIL_VAR_SSA", "HLIL_CONST", "HLIL_CONST_PTR", "HLIL_STRUCT_FIELD"})


def hlil_effect(tree, aliased: frozenset = frozenset(), allowed: frozenset = frozenset(),
                pure: Optional[Callable] = None) -> Optional[str]:
    """None if the tree only reads variables (not address-taken ones), fields of them and
    constants through supported operations (and the operation names in `allowed`); else the reason
    it is not a candidate. `pure(node)` may accept a division proven never to trap, with
    its double-width dividend (0:x)."""
    dividends: set = set()
    for node in _nodes(tree):
        name = node.operation.name
        if name in HLIL_DIVISIONS and pure is not None and pure(node):
            if name.endswith("_DP"):
                dividends.add(node.left.expr_index)
            continue
        if name == "HLIL_SPLIT" and node.expr_index in dividends:
            continue
        if name in ("HLIL_VAR", "HLIL_VAR_SSA"):
            var = getattr(node.var, "var", node.var)
            if var in aliased:
                return "effect: aliased read"
            continue
        if name == "HLIL_STRUCT_FIELD":
            if node.src.operation.name not in ("HLIL_VAR", "HLIL_VAR_SSA"):
                return "effect: HLIL_STRUCT_FIELD of a non-variable"
            continue
        if name in HLIL_LEAVES or name in _HLIL_SUPPORTED or name in allowed:
            continue
        return f"effect: {name}" if name not in _HLIL_PURE_UNSUPPORTED else f"unsupported: {name}"
    return None


HLIL_DIVISIONS = frozenset({"HLIL_DIVU", "HLIL_DIVS", "HLIL_MODU", "HLIL_MODS",
                            "HLIL_DIVU_DP", "HLIL_DIVS_DP", "HLIL_MODU_DP", "HLIL_MODS_DP"})
_HLIL_SUPPORTED = frozenset({
    "HLIL_ADD", "HLIL_SUB", "HLIL_MUL", "HLIL_AND", "HLIL_OR", "HLIL_XOR", "HLIL_LSL", "HLIL_LSR", "HLIL_ASR",
    "HLIL_NEG", "HLIL_NOT", "HLIL_ZX", "HLIL_SX", "HLIL_LOW_PART", "HLIL_ROL", "HLIL_ROR",
    "HLIL_CMP_E", "HLIL_CMP_NE", "HLIL_CMP_SLT", "HLIL_CMP_ULT", "HLIL_CMP_SLE", "HLIL_CMP_ULE", "HLIL_CMP_SGE",
    "HLIL_CMP_UGE", "HLIL_CMP_SGT", "HLIL_CMP_UGT", "HLIL_BOOL_TO_INT",
})
_HLIL_PURE_UNSUPPORTED = frozenset({
    "HLIL_TEST_BIT",
    "HLIL_RLC", "HLIL_RRC", "HLIL_ADC", "HLIL_SBB", "HLIL_BSWAP", "HLIL_POPCNT", "HLIL_CLZ", "HLIL_CTZ",
    "HLIL_RBIT", "HLIL_CLS", "HLIL_MINS", "HLIL_MAXS", "HLIL_MINU", "HLIL_MAXU", "HLIL_ABS", "HLIL_SPLIT",
    "HLIL_MULU_DP", "HLIL_MULS_DP", "HLIL_ADDRESS_OF", "HLIL_IMPORT", "HLIL_EXTERN_PTR", "HLIL_CONST_DATA",
})
