import pytest
from binja_d810.consteval.program import Address
from binja_d810.consteval.state import State, binary


def test_partial_register_writes_do_not_invent_the_upper_bits():
    state = State(16)
    state.field("r", 0x5a, 0, 8, 64)
    assert state.read("r", 8) == 0x5a
    with pytest.raises(ValueError): state.read("r", 64)
    state.field("r", 0x123456789abcde, 8, 56, 64)
    assert state.read("r", 64) == 0x123456789abcde5a


def test_logical_values_sharing_a_physical_slot_remain_distinct():
    state = State(4)
    state.assign("old", 7, 32); state.store(0, 7, 4)
    state.assign("new", 9, 32); state.store(0, 9, 4)
    assert state.read("old", 32) == 7 and state.load(0, 4) == 9


@pytest.mark.parametrize("little", (False, True))
def test_physical_bytes_and_partial_overlap_are_exact(little):
    state = State(4, little)
    state.store(0, 0x12345678, 4); state.store(1, 0x67, 1)
    assert state.load(0, 4) == (0x12346778 if little else 0x12675678)


def test_unknown_bytes_and_absolute_pointer_bits_stay_unknown():
    state = State(8)
    with pytest.raises(ValueError): state.load(0, 1)
    pointer = Address("frame", 3, 64)
    state.assign("p", pointer, 64)
    assert state.read("p", 64) == pointer
    with pytest.raises(ValueError): state.read("p", 32)
    with pytest.raises(ValueError): state.store(0, pointer, 8)
    assert binary("add", pointer, 4, 64, 64, 64) == Address("frame", 7, 64)
