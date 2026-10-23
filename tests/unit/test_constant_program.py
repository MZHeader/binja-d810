from dataclasses import replace
import random

import pytest

from binja_d810.consteval.program import AddressOf, Assign, Branch, Jump, Limits, Load, LoadIndirect, Stop, Store, StoreIndirect, run
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import BinOp, Const, UnOp, Var


def test_random_constant_keystreams_match_an_independent_python_model():
    for seed in range(128):
        rng = random.Random(seed)
        state = initial = rng.getrandbits(32)
        rotate, multiplier, add, shift = rng.randrange(1, 32), rng.getrandbits(32), rng.getrandbits(32), rng.randrange(32)
        length = rng.randrange(4, 64)
        plaintext = bytes(rng.randrange(32, 127) for _ in range(length)) + b"\0"
        program = {0: Assign("state", Const(initial, 32))}
        at = 1
        for byte in plaintext:
            k = rng.getrandbits(32)
            state = (((state >> rotate) | (state << (32 - rotate))) * multiplier + add + k) & 0xffffffff
            cipher = ((state >> shift) ^ byte) & 255
            s = Var("state", 32)
            expression = BinOp("add", BinOp("add", BinOp("mul", BinOp("ror", s, Const(rotate, 32), 32),
                                                                         Const(multiplier, 32), 32), Const(add, 32), 32), Const(k, 32), 32)
            program[at] = Assign("state", expression)
            program[at + 1] = Store("buffer", Const((at - 1) // 2, 32), UnOp("trunc", BinOp("xor",
                                       BinOp("lshr", s, Const(shift, 32), 32), Const(cipher, 32), 32), 8))
            at += 2
        program[at] = Stop()
        result = run(program, {"buffer": len(plaintext)})
        assert not result.reason and bytes(result.buffers["buffer"]) == plaintext


def loop():
    index = Var("i", 32)
    return {0: Assign("i", Const(0, 32)),
            1: Branch(BinOp("ult", index, Const(12, 32), 1), 2, 5),
            2: Store("buffer", index, UnOp("trunc", BinOp("add", index, Const(65, 32), 32), 8)),
            3: Assign("i", BinOp("add", index, Const(1, 32), 32)), 4: Jump(1), 5: Stop()}


def test_loop_execution_and_each_budget_fail_closed_without_partial_buffers():
    assert bytes(run(loop(), {"buffer": 12}).buffers["buffer"]) == b"ABCDEFGHIJKL"
    for limit, value in (("steps", 3), ("trips", 2), ("buffer_bytes", 1), ("instructions", 1), ("nodes", 1)):
        result = run(loop(), {"buffer": 12}, limits=replace(Limits(), **{limit: value}))
        assert result.reason and not result.buffers and not result.values
    assert run(loop(), {"buffer": 12}, deadline=Deadline.after(0)).reason


@pytest.mark.parametrize("bad", (Assign("x", Var("unknown", 32)), object(),
                                Load("x", "buffer", Const(0, 32), 4), Store("buffer", Const(4, 32), Const(0, 8))))
def test_unknown_inputs_effects_uninitialized_and_out_of_bounds_accesses_cancel_the_region(bad):
    result = run({0: Store("buffer", Const(0, 32), Const(7, 8)), 1: bad, 2: Stop()}, {"buffer": 4})
    assert result.reason and not result.buffers


@pytest.mark.parametrize("little_endian", (False, True))
def test_partial_memory_operations_preserve_byte_order(little_endian):
    program = {0: Store("b", Const(0, 32), Const(0x12345678, 32)), 1: Store("b", Const(1, 32), Const(0xab, 8)),
               2: Load("value", "b", Const(0, 32), 4), 3: Stop()}
    result = run(program, {"b": 4}, little_endian=little_endian)
    assert result.values["value"] == (0x1234ab78 if little_endian else 0x12ab5678)


def test_scalar_width_changes_require_an_explicit_ir_conversion():
    program = {0: Assign("x", Const(7, 8)), 1: Assign("y", Var("x", 32)), 2: Stop()}
    assert run(program, {}).reason == "constant variable width is ambiguous"
    program[1] = Assign("y", UnOp("zx", Var("x", 8), 32))
    assert run(program, {}).values["y"] == 7


def test_pointer_iterator_loops_use_buffer_identity_without_inventing_a_stack_base():
    p, end = Var("p", 64), Var("end", 64)
    program = {0: AddressOf("p", "b", Const(0, 64), 64), 1: AddressOf("end", "b", Const(12, 64), 64),
               2: Branch(BinOp("ne", p, end, 1), 3, 6), 3: StoreIndirect(p, Const(65, 8)),
               4: Assign("p", BinOp("add", p, Const(1, 64), 64)), 5: Jump(2),
               6: Assign("distance", BinOp("sub", end, p, 64)), 7: Stop()}
    result = run(program, {"b": 12})
    assert not result.reason and bytes(result.buffers["b"]) == b"A" * 12
    assert result.values["distance"] == 0


@pytest.mark.parametrize("operation", ("ult", "slt", "xor"))
def test_unknown_pointer_bits_and_order_cannot_be_evaluated(operation):
    p = Var("p", 64)
    expression = BinOp(operation, p, p, 1 if operation in ("ult", "slt") else 64)
    result = run({0: AddressOf("p", "b", Const(0, 64), 64), 1: Assign("out", expression), 2: Stop()}, {"b": 8})
    assert result.reason and not result.buffers


def test_a_pointer_cannot_be_serialized_as_a_fabricated_constant_address():
    result = run({0: AddressOf("p", "b", Const(0, 64), 64),
                  1: Store("b", Const(0, 64), Var("p", 64)), 2: Stop()}, {"b": 8})
    assert result.reason == "local pointer is observed as data" and not result.buffers
