from copy import deepcopy
from types import SimpleNamespace

import pytest

from binja_d810.deadline import Deadline
from binja_d810.summaries.verify import identical


def constant(value=3, size=4):
    return SimpleNamespace(operation=SimpleNamespace(name="LLIL_CONST"), size=size, raw_flags=0,
                           attributes=set(), detailed_operands=[("constant", value, "int")])


def assignment():
    return SimpleNamespace(operation=SimpleNamespace(name="LLIL_SET_REG"), size=4, raw_flags=0,
                           attributes={2}, detailed_operands=[("dest", SimpleNamespace(index=17), "ILRegister"),
                                                             ("src", constant(), "LowLevelILInstruction")])


def test_exact_copy_preserves_all_semantic_fields():
    node = assignment()
    assert identical(node, deepcopy(node), 8, Deadline.never())


@pytest.mark.parametrize("change", ("constant", "width", "register", "flags", "attributes", "operation", "unknown"))
def test_any_semantic_change_fails_copy_proof(change):
    before, after = assignment(), assignment()
    if change == "constant":
        after.detailed_operands[1][1].detailed_operands[0] = ("constant", 4, "int")
    elif change == "width":
        after.size = 8
    elif change == "register":
        after.detailed_operands[0][1].index = 18
    elif change == "flags":
        after.raw_flags = 1
    elif change == "attributes":
        after.attributes.clear()
    elif change == "operation":
        after.operation.name = "LLIL_STORE"
    else:
        after.detailed_operands.append(("unsupported", object(), "unknown"))
    assert not identical(before, after, 8, Deadline.never())


def test_copy_proofs_obey_work_and_time_limits():
    before, after = assignment(), assignment()
    assert not identical(before, after, 1, Deadline.never())
    assert not identical(before, after, 8, Deadline.after(0))
