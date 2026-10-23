"""Static imported-allocation fixtures under explicit test contracts; never executed."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from binaryninja import MediumLevelILOperation as M, Settings
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, set_types, settle
from flowcases import text
from test_scratch import value_ir

from binja_d810.allocations.contracts import ATTRIBUTE
from binja_d810.binja import allocation_roundtrips as allocations, log, settings
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import BinOp, Const
from binja_d810.semantic.verify import Verdict, check_equivalent

NAMES = ("heap_roundtrip", "heap_box", "heap_partial", "heap_escape", "heap_failure", "heap_merge", "heap_observed_bits")


def test_long_pointer_arithmetic_is_iterative_and_width_exact():
    identity = object()
    obj = allocations.Object.__new__(allocations.Object)
    obj.aliases = {identity: 5}
    visits = []
    obj.factory = SimpleNamespace(tick=lambda: visits.append(1), il=SimpleNamespace(arch=SimpleNamespace(address_size=8)))
    node = SimpleNamespace(operation=M.MLIL_VAR_SSA, src=identity, size=8)
    for _ in range(2000):
        node = SimpleNamespace(operation=M.MLIL_ADD, size=8, left=node,
                               right=SimpleNamespace(operation=M.MLIL_CONST, size=8, constant=3))
    assert obj.address(node) == 6005 and len(visits) == 2001
    node.right.size = 4
    assert obj.address(node) is None
    assert obj.address(SimpleNamespace(operation=M.MLIL_VAR_SSA, src=identity, size=4)) is None


def configure(bv, *, contracts=True, nonnull=True, elidable=True):
    size = "uint64_t" if bv.address_size == 8 else "uint32_t"
    types = {name: f"uint32_t {name}(uint32_t x)" for name in NAMES}
    types.update(heap_allocate=f"void *heap_allocate({size} size)", heap_release="void heap_release(void *p)",
                 heap_small="void *heap_small(void)", heap_drop="void heap_drop(void *p)")
    set_types(bv, types)
    if contracts:
        for name, role in (("heap_allocate", "allocate"), ("heap_release", "deallocate")):
            spec = dict(version=1, role=role, parameter=0, elidable=elidable)
            if role == "allocate":
                spec.update(fresh=True, ordinary=True, nonnull=nonnull)
            # These are explicit test assumptions, not guarantees inferred for real malloc.
            function_named(bv, name).store_metadata(ATTRIBUTE, json.dumps(spec))
    for func in bv.functions: func.reanalyze()
    settle(bv)


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    on = open_sample(sample("allocation_roundtrips", *request.param), d810=True)
    off = open_sample(sample("allocation_roundtrips", *request.param), d810=True, options={settings.ALLOCATIONS: False})
    for bv in (on, off): configure(bv)
    yield request.param, on, off
    on.file.close()
    off.file.close()


def calls(func):
    return sum("CALL" in i.operation.name for i in func.mlil.instructions)


def expected(name, x):
    if name == "heap_roundtrip":
        return BinOp("xor", x, Const(0x51, 32), 32)
    if name == "heap_box":
        return BinOp("add", BinOp("mul", x, Const(7, 32), 32), Const(3, 32), 32)
    return BinOp("or", BinOp("and", x, Const(0xffff00ff, 32), 32), Const(0x6700, 32), 32)


@pytest.mark.parametrize("name", NAMES[:3])
def test_explicit_contracts_remove_private_round_trips_with_exact_values(builds, name):
    _, on, off = builds
    func = function_named(on, name)
    assert calls(function_named(off, name)) == 2
    assert calls(func) == 0, (text(func), log.render(log.latest_report(func)))
    expression, x = value_ir(func)
    assert check_equivalent(expression, expected(name, x)) is Verdict.PROVEN
    report = log.latest_report(func)
    assert report.allocation_lifetimes == 1 and report.allocation_loads >= 1
    assert "allocations:" in log.render(report)


@pytest.mark.parametrize("name", NAMES[3:])
def test_escapes_and_observed_failure_paths_stay_unchanged(builds, name):
    _, on, off = builds
    func = function_named(on, name)
    assert text(func) == text(function_named(off, name))
    assert not log.latest_report(func).allocation_lifetimes


@pytest.mark.parametrize("limit", ("instructions", "nodes", "bytes", "contract_bytes"))
def test_allocation_limits_and_expired_deadlines_reject_edits(builds, limit):
    _, _, off = builds
    il = function_named(off, "heap_roundtrip").mlil
    new, report = allocations.run(il, replace(allocations.Limits(), **{limit: 1}), Deadline.after(2))
    assert new is il and not report.loads and not report.lifetimes and report.notes
    new, report = allocations.run(il, deadline=Deadline.after(0))
    assert new is il and report.exhausted


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
@pytest.mark.parametrize("mode", ("missing", "nullable", "effects"))
def test_missing_failure_and_effect_guarantees_are_not_invented(build, mode):
    bv = open_sample(sample("allocation_roundtrips", *build), d810=True)
    try:
        configure(bv, contracts=mode != "missing", nonnull=mode != "nullable", elidable=mode != "effects")
        func = function_named(bv, "heap_roundtrip")
        report = log.latest_report(func)
        assert calls(func) == 2 and not report.allocation_lifetimes
        assert report.allocations
        if mode == "effects":
            assert report.allocation_loads == 1
            assert any(i.operation in (M.MLIL_STORE, M.MLIL_STORE_STRUCT) for i in func.mlil.instructions)
            expression, x = value_ir(func)
            assert check_equivalent(expression, expected("heap_roundtrip", x)) is Verdict.PROVEN
        else:
            assert not report.allocation_loads
    finally:
        bv.file.close()


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_disable_restores_calls_without_changing_user_contracts(build):
    bv = open_sample(sample("allocation_roundtrips", *build), d810=True, options={settings.ALLOCATIONS: False})
    try:
        configure(bv)
        baseline = {name: text(function_named(bv, name)) for name in NAMES}
        metadata = function_named(bv, "heap_allocate").get_metadata(ATTRIBUTE)
        for enabled in (True, False):
            Settings().set_bool(settings.ALLOCATIONS, enabled, bv)
            for func in bv.functions: func.reanalyze()
            settle(bv)
        assert {name: text(function_named(bv, name)) for name in NAMES} == baseline
        assert function_named(bv, "heap_allocate").get_metadata(ATTRIBUTE) == metadata
    finally:
        bv.file.close()
