"""Effect policy tables, tested on stand-in IL nodes: no Binary Ninja needed."""

from dataclasses import dataclass, field

import pytest

from binja_d810.binja import effects


@dataclass
class Op:
    name: str


@dataclass
class Node:
    """Duck-types the parts of a Binary Ninja IL instruction the policy reads."""

    op: str
    operands: list = field(default_factory=list)

    @property
    def operation(self):
        return Op(self.op)


def var():
    return Node("MLIL_VAR_SSA", ["ssa-var"])


def const():
    return Node("MLIL_CONST", [7])


@pytest.mark.parametrize("op", ["MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT_SSA", "MLIL_VAR_ALIASED", "MLIL_VAR_ALIASED_FIELD",
                                "MLIL_CALL_SSA", "MLIL_INTRINSIC_SSA", "MLIL_DIVU", "MLIL_MODS", "MLIL_UNDEF",
                                "MLIL_UNIMPL_MEM", "MLIL_FADD", "MLIL_SOMETHING_NEW"])
def test_effectful_or_unknown_operations_are_rejected(op):
    tree = Node("MLIL_ADD", [var(), Node(op, [var()])])
    assert effects.mlil_effect(tree) == f"effect: {op}"


@pytest.mark.parametrize("op", ["MLIL_VAR_SSA_FIELD", "MLIL_ADDRESS_OF", "MLIL_CMP_E", "MLIL_BOOL_TO_INT", "MLIL_ROL",
                                "MLIL_POPCNT", "MLIL_BSWAP", "MLIL_ADC", "MLIL_SBB", "MLIL_CONST_PTR", "MLIL_IMPORT"])
def test_pure_operations_are_safe_to_reevaluate(op):
    tree = Node("MLIL_XOR", [var(), Node(op, [var(), const()])])
    assert effects.mlil_effect(tree) is None


def test_effects_are_found_at_any_depth():
    deep = Node("MLIL_ADD", [var(), Node("MLIL_ROL", [Node("MLIL_NOT", [Node("MLIL_LOAD_SSA", [var()])]), const()])])
    assert effects.mlil_effect(deep) == "effect: MLIL_LOAD_SSA"


def test_lists_of_operands_are_searched():
    assert effects.mlil_effect(Node("MLIL_ADD", [[var(), Node("MLIL_CALL_SSA")], const()])) == "effect: MLIL_CALL_SSA"


def hvar(name="x"):
    return Node("HLIL_VAR", [name]) if False else _HVar(name)


class _HVar(Node):
    def __init__(self, name):
        super().__init__("HLIL_VAR", [])
        self.var = name


def test_hlil_reads_of_plain_variables_are_candidates():
    tree = Node("HLIL_ADD", [Node("HLIL_XOR", [_HVar("x"), _HVar("y")]), Node("HLIL_CONST", [3])])
    assert effects.hlil_effect(tree) is None


@pytest.mark.parametrize("op", ["HLIL_DEREF", "HLIL_CALL", "HLIL_ARRAY_INDEX", "HLIL_DIVU", "HLIL_INTRINSIC", "HLIL_NEW_THING"])
def test_hlil_effects_bound_candidates(op):
    assert effects.hlil_effect(Node("HLIL_ADD", [_HVar("x"), Node(op, [_HVar("y")])])) == f"effect: {op}"


def test_hlil_pure_unsupported_operations_bound_candidates_too():
    assert effects.hlil_effect(Node("HLIL_ADD", [_HVar("x"), Node("HLIL_POPCNT", [_HVar("y")])])) == "unsupported: HLIL_POPCNT"


def test_hlil_aliased_variable_reads_are_effects():
    assert effects.hlil_effect(Node("HLIL_XOR", [_HVar("x_1"), _HVar("y")]), aliased=frozenset({"x_1"})) == \
        "effect: aliased read"
