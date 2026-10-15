"""Effect policy tables (spec section 5.2), tested on stand-in IL nodes: no Binary Ninja needed."""

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
