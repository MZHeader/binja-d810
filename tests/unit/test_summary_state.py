"""Independent bit-vector models for private register views and summary composition."""

import random

import pytest

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import BinOp, Const, UnOp, Var, mask
from binja_d810.semantic.verify import Verdict, check
from binja_d810.summaries.state import Register, State


@pytest.mark.parametrize("whole", (16, 32, 64, 128))
def test_partial_writes_keep_every_other_bit(whole):
    rng = random.Random(whole)
    for width in (8, 16, 32, 64):
        if width > whole:
            continue
        for offset in range(0, whole - width + 1, 8):
            state = State({"r": whole, "v": width})
            state.write(Register("r", width, offset), Var("v", width))
            for _ in range(20):
                r, v = rng.getrandbits(whole), rng.getrandbits(width)
                field = mask(width) << offset
                assert evaluate(state.outputs()["r"], {"r": r, "v": v}) == (r & ~field) | (v << offset)


@pytest.mark.parametrize("extend", ("zero", "sign"))
def test_implicit_extension_is_proven_at_the_full_width(extend):
    state = State({"r": 64, "v": 32})
    state.write(Register("r", 32, extend=extend), Var("v", 32))
    reference = UnOp("zx" if extend == "zero" else "sx", Var("v", 32), 64)
    assert check(state.outputs()["r"], reference).verdict is Verdict.PROVEN


def test_summary_outputs_are_bound_simultaneously():
    state = State({"a": 64, "b": 64})
    state.apply({"a": Var("b", 64), "b": Var("a", 64)})
    assert [evaluate(state.outputs()[r], {"a": 11, "b": 29}) for r in ("a", "b")] == [29, 11]


def test_live_inputs_and_outputs_come_from_the_body():
    state = State({"hidden": 64, "scratch": 64, "out": 64, "unused": 64})
    state.write(Register("scratch", 64), Const(17, 64))
    result = BinOp("add", state.read(Register("hidden", 64)), state.read(Register("scratch", 64)), 64)
    state.write(Register("out", 64), result)
    assert state.live_in == {"hidden"}
    assert set(state.outputs()) == {"scratch", "out"}
    assert evaluate(state.outputs()["out"], {"hidden": 31}) == 48


@pytest.mark.parametrize("reg", (Register("r", 16, 24), Register("r", 16, -1),
                                  Register("r", 16, 8, "zero"), Register("r", 16, 0, "unknown")))
def test_invalid_register_views_are_rejected(reg):
    state = State({"r": 32})
    with pytest.raises(ValueError):
        state.write(reg, Const(1, 16))


def test_summary_width_mismatches_leave_state_unchanged():
    state = State({"a": 64, "b": 32})
    with pytest.raises(ValueError):
        state.apply({"a": Var("b", 64)})
    assert not state.written
    with pytest.raises(ValueError):
        state.apply({"b": Const(1, 64)})
    assert not state.written
