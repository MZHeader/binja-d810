"""C-generated arithmetic flags and native compare chains, with independent value models."""

from dataclasses import replace

import pytest
from binaryninja import Settings
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, set_types, settle
from flowcases import text
from flag_model import execute, proves
import z3

from binja_d810.binja import flag_conditions, log, settings
from binja_d810.deadline import Deadline
from binja_d810.flags.recombine import Limits
from binja_d810.ir.expr import CMP_OPS

SIMPLE = tuple("flag_" + op for op in sorted(CMP_OPS))
CHAINS = ("flag_signed_chain", "flag_fixed_chain", "flag_unsigned_chain", "flag_nested_chain")
TYPES = {name: f"uint32_t {name}(uint32_t x, uint32_t y)" for name in SIMPLE + ("flag_shared",)}
TYPES["flag_fixed_chain"] = "uint32_t flag_fixed_chain(int32_t x, int32_t y)"
for name in ("flag_unsigned_chain", "flag_nested_chain"):
    TYPES[name] = f"uint32_t {name}(uint32_t x, uint32_t y, uint32_t z)"
TYPES["flag_signed_chain"] = "uint32_t flag_signed_chain(int32_t x, int32_t y, int32_t z)"


def test_flag_settings_are_registered_with_the_documented_defaults():
    values = Settings()
    assert values.get_bool(settings.FLAGS)
    assert values.get_bool(settings.FLAG_SOURCE)
    for key, default in ((settings.FLAG_INSTRUCTIONS, 20000), (settings.FLAG_NODES, 2000),
                         (settings.FLAG_WORK, 80000), (settings.FLAG_CANDIDATES, 128),
                         (settings.FLAG_CONDITIONS, 128), (settings.FLAG_MERGES, 8),
                         (settings.FLAG_PROOF_MS, 200), (settings.FLAG_ROUNDS, 3), (settings.FLAG_SOURCE_BITS, 64)):
        assert values.contains(key) and values.get_integer(key) == default


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    on = open_sample(sample("flag_conditions", *request.param), d810=True)
    off = open_sample(sample("flag_conditions", *request.param), d810=True, options={settings.FLAGS: False})
    for bv in (on, off):
        set_types(bv, TYPES)
        settle(bv)
    yield request.param, on, off
    on.file.close()
    off.file.close()


@pytest.mark.parametrize("name", SIMPLE + CHAINS)
def test_compiled_flags_match_independent_comparisons(builds, name):
    build, on, off = builds
    func = function_named(on, name)
    assert proves(func, name), text(func)


def test_emitted_arithmetic_flag_logic_is_actually_recombined(builds):
    _, on, off = builds
    candidates = [function_named(on, "flag_" + op) for op in ("slt", "sle", "sgt", "sge")]
    assert sum(r.flag_conditions for f in candidates for r in log.latest_reports(f).values()) > 0
    assert any(text(f) != text(function_named(off, f.name.lstrip("_"))) for f in candidates)
    assert any("flags:" in log.render(r) for f in candidates for r in log.latest_reports(f).values())


@pytest.mark.parametrize("name", ("flag_signed_chain", "flag_fixed_chain", "flag_unsigned_chain"))
def test_native_conditional_compare_merges_disappear_on_arm64(builds, name):
    build, on, off = builds
    if build != ("arm64", "O2"):
        pytest.skip("this compiler emits the conditional-compare merge on ARM64 O2")
    func = function_named(on, name)
    assert len(list(func.hlil.instructions)) == 1, text(func)
    assert len(list(function_named(off, name).hlil.instructions)) > 1
    assert sum(r.flag_conditions for r in log.latest_reports(func).values()) > 0
    assert proves(func, name)


def test_flag_values_used_as_data_keep_their_observable_store(builds):
    _, on, off = builds
    func = function_named(on, "flag_shared")
    # Other independent conditions may simplify. The shared sign flag stays observable.
    x, y = z3.BitVec("x", 32), z3.BitVec("y", 32)
    before = execute(function_named(off, "flag_shared"))
    for path, _, writes in execute(func):
        assert len(writes) == 1
        solver = z3.Solver()
        solver.add(path, writes[0][1] != z3.LShR(x - y, 31))
        assert solver.check() == z3.unsat
        assert any(z3.eq(writes[0][0], original[0][0]) for _, _, original in before if original)
    assert proves(func, "flag_sle")
    assert any("other uses" in note for r in log.latest_reports(func).values() for note in r.flags)


@pytest.mark.parametrize("limit", ("instructions", "nodes", "work"))
def test_flag_caps_and_shared_deadline_reject_unproved_changes(builds, limit):
    _, _, off = builds
    func = function_named(off, "flag_sle")
    original = text(func)
    report = flag_conditions.run(func.hlil, "hlil", replace(Limits(), **{limit: 1}), Deadline.after(2))
    assert not report.conditions and report.notes
    assert text(func) == original
    report = flag_conditions.run(func.hlil, "hlil", Limits(), Deadline.after(0))
    assert not report.conditions and report.exhausted


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_disabling_flags_restores_the_original_conditions(build):
    bv = open_sample(sample("flag_conditions", *build), d810=True, options={settings.FLAGS: False})
    try:
        set_types(bv, TYPES)
        settle(bv)
        baseline = {name: text(function_named(bv, name)) for name in SIMPLE + CHAINS}
        for enabled in (True, False):
            Settings().set_bool(settings.FLAGS, enabled, bv)
            for func in bv.functions: func.reanalyze()
            settle(bv)
        assert {name: text(function_named(bv, name)) for name in SIMPLE + CHAINS} == baseline
    finally:
        bv.file.close()
