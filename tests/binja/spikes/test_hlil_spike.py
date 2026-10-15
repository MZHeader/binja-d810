"""HLIL spike gates (spec section 5.5). Evidence for docs/design/spikes/2026-10-15-v2-hlil.md.

Run: BINJA_D810_SPIKE=1 scripts/binja_pytest.sh tests/binja/spikes/test_hlil_spike.py
"""

import json
import os
from pathlib import Path

import binaryninja
import hlil_spike
import oracle
import pytest
from binaryninja import HighLevelILOperation as H, Settings
from helpers import BUILD, function_named, manifest, set_types, signature

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test"))
EVIDENCE: dict = {}
ENTRIES = {e["name"]: e for e in manifest()}
REUSE = ["mba_linear_001", "mba_linear_002", "mba_linear_006", "mba_linear_013", "mba_linear_030"]
FIXTURE_TYPES = {
    "cf_branch": "uint32_t cf_branch(uint32_t x, uint32_t y, uint32_t c)",
    "cf_loop": "uint32_t cf_loop(uint32_t x, uint32_t y, uint32_t n)",
    "cf_reassign": "uint32_t cf_reassign(uint32_t x, uint32_t y)",
    "hl_call_inline": "uint32_t hl_call_inline(uint32_t x, uint32_t y)",
    "hl_volatile": "uint32_t hl_volatile(uint32_t y)",
    "hl_select": "uint32_t hl_select(uint32_t x, uint32_t y, uint32_t c)",
}
BITWISE = ("^", " & ", " | ", "~")


@pytest.fixture(autouse=True, scope="module")
def _registered():
    hlil_spike.register()
    yield
    (OUT / "hlil_spike_evidence.json").write_text(json.dumps(EVIDENCE, indent=1, default=str))


@pytest.fixture(autouse=True)
def _reset():
    hlil_spike.FAIL_AT["stage"] = None
    hlil_spike.MODE.update(gate_on_toggle=True, remove_dead=True, enabled=True)
    yield
    hlil_spike.FAIL_AT["stage"] = None
    hlil_spike.MODE.update(gate_on_toggle=True, remove_dead=True, enabled=True)


def _load(name, *, toggle=True, types=None):
    bv = binaryninja.load(str(BUILD / name), options={hlil_spike.MLIL_TOGGLE: toggle})
    set_types(bv, types or {n: signature(ENTRIES[n]) for n in REUSE})
    return bv


def _hlil(func) -> str:
    return "\n".join(map(str, func.hlil.instructions))


def _pseudo_c(func) -> str:
    return "\n".join(str(line) for line in func.pseudo_c.get_linear_lines(func.hlil.root))


def _reanalyze(bv, func):
    func.reanalyze()
    bv.update_analysis_and_wait()
    return bv.get_function_at(func.start)


def _count(func, ops) -> int:
    n = 0
    for statement in func.hlil.instructions:
        stack = [statement]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif hasattr(node, "operation"):
                n += node.operation in ops
                stack.extend(node.operands)
    return n


def test_gate1_rewrite_persists_into_hlil_and_pseudo_c_and_survives_reanalysis():
    bv = _load("mba_O2.dylib")
    try:
        for name in REUSE:
            func = function_named(bv, name)
            assert hlil_spike.LOG[func.start][-1]["stage"] == "done", hlil_spike.LOG[func.start][-1]
            expected_ops = sum(ENTRIES[name]["expected"].count(op.strip()) for op in BITWISE)
            for text in (_hlil(func), _pseudo_c(func)):
                assert sum(text.count(op.strip()) for op in BITWISE) <= expected_ops, text
            func = _reanalyze(bv, func)
            assert hlil_spike.LOG[func.start][-1]["stage"] == "done"
            EVIDENCE.setdefault("gate1", {})[name] = {"hlil": _hlil(func), "pseudo_c": _pseudo_c(func)}
            final = oracle.function_result(func)
            entry = ENTRIES[name]
            kind = oracle.compare(final, oracle.reference(entry["expected"], entry["width"]), entry["width"]).kind
            assert kind in ("proven", "exhaustive"), (name, kind)
    finally:
        bv.file.close()


def test_gate2_reads_after_mlil_register_reuse_resolve_to_the_parameters():
    bv = _load("mba_O2.dylib")
    try:
        for name in REUSE:
            func = function_named(bv, name)
            ssa = func.hlil.ssa_form
            (ret,) = [i for i in ssa.instructions if i.operation == H.HLIL_RET]
            reads = []
            stack = list(ret.src)
            while stack:
                node = stack.pop()
                if node.operation == H.HLIL_VAR_SSA:
                    reads.append(node.var)
                stack.extend(o for o in node.operands if hasattr(o, "operation"))
            assert reads, name
            for ssa_var in reads:
                # Entry parameters: version 0, no defining instruction, and a parameter variable;
                # a newly introduced undefined version would have version > 0.
                assert ssa_var.var in func.parameter_vars, (name, ssa_var)
                assert ssa_var.version == 0 and ssa.get_ssa_var_definition(ssa_var) is None, (name, ssa_var)
            EVIDENCE.setdefault("gate2", {})[name] = [str(r) for r in reads]
    finally:
        bv.file.close()


@pytest.mark.parametrize("level", ["O0", "O2"])
def test_gate2_versions_are_per_variable_and_reassignment_is_respected(level):
    bv = _load(f"hlil_fixtures_{level}.dylib", types=FIXTURE_TYPES)
    try:
        func = function_named(bv, "cf_reassign")
        log = hlil_spike.LOG.get(func.start, [])
        EVIDENCE.setdefault("gate2_reassign", {})[level] = {"hlil": _hlil(func), "log": log[-1:] if log else None}
        for entry in log:
            for rewrite in entry["rewrites"]:
                for name, leaf_version, root_version, _ in rewrite["leaves"]:
                    # The spike lifts without the version check; record whether it would have been needed.
                    EVIDENCE.setdefault("gate2_leaf_versions", []).append((level, name, leaf_version, root_version))
        final = oracle.function_result(func)
        assert oracle.compare(final, oracle.reference("4 * x + y + 1", 32), 32).kind in ("proven", "exhaustive")
    finally:
        bv.file.close()


def test_gate3_dead_assignments_can_be_removed():
    bv = _load("mba_O2.dylib")
    try:
        func = function_named(bv, "mba_linear_001")
        log = hlil_spike.LOG[func.start][-1]
        assert log["removed"], log
        assert [str(i) for i in func.hlil.instructions] == ["return x + y"], _hlil(func)
    finally:
        bv.file.close()


def test_gate4_one_toggle_enables_both_activities():
    bv = _load("mba_O2.dylib", toggle=False)
    try:
        func = function_named(bv, "mba_linear_001")
        assert func.start in hlil_spike.GENERATIONS  # the HLIL activity ran ...
        assert any(op in _hlil(func) for op in BITWISE)  # ... and returned at once: toggle off
        Settings().set_bool(hlil_spike.MLIL_TOGGLE, True, func)
        func = _reanalyze(bv, func)
        assert _hlil(func) == "return x + y"
    finally:
        bv.file.close()


def test_gate4_generation_identity():
    bv = _load("mba_O2.dylib")
    try:
        func = function_named(bv, "mba_linear_001")
        before = list(hlil_spike.GENERATIONS[func.start])
        func = _reanalyze(bv, func)
        after = hlil_spike.GENERATIONS[func.start][len(before):]
        EVIDENCE["gate4_generations"] = {"first": before, "reanalysis": after}
        mlil_records = [h for kind, h, _ in after if kind == "mlil"]
        hlil_records = [(m, h) for kind, m, h in after if kind == "hlil"]
        assert mlil_records and hlil_records
        # Within one analysis run the HLIL activity sees the MLIL the MLIL activity produced.
        assert hlil_records[-1][0] == mlil_records[-1]
    finally:
        bv.file.close()


@pytest.mark.parametrize("stage", ["replace", "cleanup", "finalize", "ssa"])
def test_gate5_injected_failures_restore_the_pre_pass_hlil(stage):
    names = ["mba_linear_001", "mba_linear_013"]
    fixtures = ["cf_branch", "cf_loop", "hl_select"]
    hlil_spike.MODE["enabled"] = False
    ref_mba = _load("mba_O2.dylib")
    ref_fix = _load("hlil_fixtures_O2.dylib", types=FIXTURE_TYPES)
    hlil_spike.MODE["enabled"] = True
    hlil_spike.FAIL_AT["stage"] = stage
    bv_mba = _load("mba_O2.dylib")
    bv_fix = _load("hlil_fixtures_O2.dylib", types=FIXTURE_TYPES)
    try:
        for ref, bv, group in ((ref_mba, bv_mba, names), (ref_fix, bv_fix, fixtures)):
            for name in group:
                func, expected = function_named(bv, name), function_named(ref, name)
                log = hlil_spike.LOG.get(func.start, [{}])[-1]
                if not log.get("rewrites"):
                    continue
                assert log.get("rolled_back"), (name, log)
                assert _hlil(func) == _hlil(expected), (stage, name)
                assert [str(v) for v in func.hlil.aliased_vars] == [str(v) for v in expected.hlil.aliased_vars]
                EVIDENCE.setdefault("gate5", {}).setdefault(stage, []).append(name)
    finally:
        for bv in (ref_mba, ref_fix, bv_mba, bv_fix):
            bv.file.close()


def test_gate6_cross_il_mappings_after_edit():
    bv = _load("mba_O2.dylib")
    try:
        for name in REUSE:
            func = function_named(bv, name)
            (ret,) = [i for i in func.hlil.instructions if i.operation == H.HLIL_RET]
            mlil = ret.mlil
            record = {"hlil_ret": str(ret), "mlil": str(mlil) if mlil is not None else None,
                      "mlils": [str(m) for m in ret.mlils], "address": hex(ret.address)}
            mlil_ret = [i for i in func.mlil.instructions if i.operation.name == "MLIL_RET"][0]
            record["mlil_ret_to_hlil"] = str(mlil_ret.hlil) if mlil_ret.hlil is not None else None
            EVIDENCE.setdefault("gate6", {})[name] = record
            assert mlil is not None, record
            assert mlil_ret.hlil is not None, record
    finally:
        bv.file.close()


@pytest.mark.parametrize("level", ["O0", "O2"])
def test_gate7_effects_are_preserved(level):
    hlil_spike.MODE["enabled"] = False
    ref = _load(f"hlil_fixtures_{level}.dylib", types=FIXTURE_TYPES)
    hlil_spike.MODE["enabled"] = True
    bv = _load(f"hlil_fixtures_{level}.dylib", types=FIXTURE_TYPES)
    try:
        calls = {H.HLIL_CALL, H.HLIL_CALL_SSA, H.HLIL_TAILCALL}
        derefs = {H.HLIL_DEREF, H.HLIL_DEREF_FIELD, H.HLIL_ARRAY_INDEX}
        branches = {H.HLIL_IF, H.HLIL_WHILE, H.HLIL_DO_WHILE, H.HLIL_FOR, H.HLIL_SWITCH}
        for name in FIXTURE_TYPES:
            func, plain = function_named(bv, name), function_named(ref, name)
            assert _count(func, calls) == _count(plain, calls), name
            assert _count(func, derefs) == _count(plain, derefs), name
            assert _count(func, branches) == _count(plain, branches), name
            EVIDENCE.setdefault("gate7", {}).setdefault(level, {})[name] = {"before": _hlil(plain), "after": _hlil(func)}
    finally:
        bv.file.close()
        ref.file.close()


@pytest.mark.parametrize("level", ["O0", "O2"])
def test_gate2_hlil_versions_with_the_mlil_pass_off(level):
    # The kill switch stops the MLIL pass; the spike still runs, so HLIL holds the original MBA.
    Settings().set_bool("binja_d810.enabled", False)
    try:
        bv = _load(f"hlil_fixtures_{level}.dylib", types=FIXTURE_TYPES)
        try:
            func = function_named(bv, "cf_reassign")
            log = hlil_spike.LOG.get(func.start, [{}])[-1]
            leaves = [leaf for r in log.get("rewrites", []) for leaf in r["leaves"]]
            EVIDENCE.setdefault("gate2_mlil_off", {})[level] = {"hlil": _hlil(func), "leaves": leaves}
            final = oracle.function_result(func)
            assert oracle.compare(final, oracle.reference("4 * x + y + 1", 32), 32).kind in ("proven", "exhaustive")
        finally:
            bv.file.close()
    finally:
        Settings().reset("binja_d810.enabled")


@pytest.mark.parametrize("check", [False, True])
def test_gate2_leaf_version_check_on_a_loop_carried_value(check):
    # cf_loop at -O2: x10_1 = y ^ x and x11_1 = y & x inside the loop, then y is reassigned, and
    # the return combines x10_1 + (x11_1 << 1). Inlining gives y + x over the *loop's* y, which is
    # not the y current at the return.
    Settings().set_bool("binja_d810.enabled", False)
    hlil_spike.CHECK_VERSIONS["on"] = check
    try:
        bv = _load("hlil_fixtures_O2.dylib", types=FIXTURE_TYPES)
        try:
            func = function_named(bv, "cf_loop")
            log = hlil_spike.LOG.get(func.start, [{}])[-1]
            stale = [leaf for r in log.get("rewrites", []) for leaf in r["leaves"] if leaf[1] != leaf[2]]
            EVIDENCE.setdefault("gate2_loop", {})[str(check)] = {"hlil": _hlil(func), "log": log, "stale_leaves": stale}
            if check:
                assert not any(leaf[1] != leaf[2] for r in log.get("rewrites", []) for leaf in r["leaves"]), log
        finally:
            bv.file.close()
    finally:
        hlil_spike.CHECK_VERSIONS["on"] = False
        Settings().reset("binja_d810.enabled")
