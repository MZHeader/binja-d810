"""Proofs remembered by shape: a verdict cannot change when variables are renamed."""

import time

from binja_d810.deadline import Deadline
from binja_d810.ir.expr import BinOp, Const, Var, shape_key
from binja_d810.ir.parse import parse
from binja_d810.semantic import constancy, verify
from binja_d810.semantic.verify import Verdict, check


def test_shape_keys_ignore_names_and_keep_widths_and_structure():
    x, y, z = Var("x", 32), Var("y", 32), Var("z", 8)
    assert shape_key(BinOp("add", x, Const(1, 32), 32)) == shape_key(BinOp("add", y, Const(1, 32), 32))
    assert shape_key(BinOp("add", z, Const(1, 8), 8)) != shape_key(BinOp("add", x, Const(1, 32), 32))
    assert shape_key(BinOp("add", x, y, 32)) != shape_key(BinOp("add", x, x, 32))
    # Across several expressions: a variable shared by both sides stays one variable.
    assert shape_key(x, BinOp("add", x, y, 32)) == shape_key(y, BinOp("add", y, x, 32))
    assert shape_key(x, BinOp("add", x, y, 32)) != shape_key(x, BinOp("add", y, x, 32))
    # One name at two widths is two variables.
    assert shape_key(BinOp("add", Var("x", 8), Const(1, 8), 8), Var("x", 32)) == \
        shape_key(BinOp("add", Var("a", 8), Const(1, 8), 8), Var("b", 32))


def test_shape_keys_keep_shared_values_shared():
    t = Var("t", 32)
    for i in range(30):  # 2^30 nodes as a tree
        m = Const(0x9E3779B9 * (i + 1), 32)
        t = BinOp("or", BinOp("and", t, m, 32), BinOp("and", BinOp("xor", t, Const(-1, 32), 32),
                                                        BinOp("xor", m, Const(-1, 32), 32), 32), 32)
    started = time.perf_counter()
    key = shape_key(t)
    assert key == shape_key(t) and time.perf_counter() - started < 0.5


def _predicate(a: str, b: str):
    return parse(f"ne(udiv(2, ({a} * {a}) + 1), ({b} * {b}) + 3)", 1, {a: 32, b: 32})


def test_check_remembers_proofs_by_shape(monkeypatch):
    assert check(_predicate("x", "y"), Const(1, 1)).verdict is Verdict.PROVEN

    def no_solver(*args, **kwargs):
        raise AssertionError("the solver ran again for the same shape")

    monkeypatch.setattr(verify, "_z3_prove", no_solver)
    assert check(_predicate("ecx_16", "esi_8"), Const(1, 1)).verdict is Verdict.PROVEN


def test_check_does_not_remember_inconclusive_outcomes(monkeypatch):
    real = verify._z3_prove
    monkeypatch.setattr(verify, "_z3_prove", lambda *args, **kwargs: (None, "z3 timeout"))
    assert check(_predicate("p", "q"), Const(1, 1)).verdict is Verdict.TESTED
    monkeypatch.setattr(verify, "_z3_prove", real)
    assert check(_predicate("p", "q"), Const(1, 1)).verdict is Verdict.PROVEN


def test_fold_shared_remembers_by_shape(monkeypatch):
    found = constancy.fold_shared(_predicate("u", "v"), Deadline.never())
    assert found is not None and found[0] == 1 and found[1].verdict is Verdict.PROVEN

    def no_solver(*args, **kwargs):
        raise AssertionError("the solver ran again for the same shape")

    monkeypatch.setattr(constancy, "_z3_prove", no_solver)
    again = constancy.fold_shared(_predicate("s", "t"), Deadline.never())
    assert again is not None and again[0] == 1 and again[1].verdict is Verdict.PROVEN
    # Not constant: remembered as such too (two samples differ).
    assert constancy.fold_shared(parse("eq(x & 7, y)", 1, {"x": 32, "y": 32}), Deadline.never()) is None


def test_fold_shared_does_not_remember_inconclusive_outcomes(monkeypatch):
    real = constancy._z3_prove
    monkeypatch.setattr(constancy, "_z3_prove", lambda *args, **kwargs: (None, "z3 timeout"))
    first = constancy.fold_shared(_predicate("g", "h"), Deadline.never())
    assert first is not None and first[1].verdict is Verdict.TESTED
    monkeypatch.setattr(constancy, "_z3_prove", real)
    second = constancy.fold_shared(_predicate("g", "h"), Deadline.never())
    assert second is not None and second[1].verdict is Verdict.PROVEN
