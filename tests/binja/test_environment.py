import subprocess

import binaryninja
import pytest
from binaryninja import Workflow

from binja_d810.binja import workflow

SOURCE = "int probe(int a, int b) { return (a ^ b) + 2 * (a & b); }\n"


def _build(tmp_path) -> str:
    src = tmp_path / "probe.c"
    src.write_text(SOURCE)
    out = tmp_path / "probe.dylib"
    subprocess.run(["clang", "-arch", "arm64", "-O0", "-shared", "-o", str(out), str(src)], check=True)
    return str(out)


def _probe_function(bv):
    return next(f for f in bv.functions if f.name.lstrip("_") == "probe")


def test_activity_registered_on_meta_analysis():
    assert Workflow("core.function.metaAnalysis").contains(workflow.ACTIVITY_NAME)


def test_activity_registered_on_objective_c_when_present():
    if "core.function.objectiveC" not in Workflow:
        pytest.skip("this build has no core.function.objectiveC workflow")
    assert Workflow("core.function.objectiveC").contains(workflow.ACTIVITY_NAME)


def test_auto_setting_identifier():
    keys = Workflow("core.function.metaAnalysis").eligibility_settings()
    assert workflow.ACTIVITY_SETTING in keys, keys


def test_activity_off_by_default(tmp_path):
    workflow.PROBE.clear()
    bv = binaryninja.load(_build(tmp_path))
    try:
        assert _probe_function(bv).start not in workflow.PROBE
    finally:
        bv.file.close()


def test_enabled_activity_runs_with_ssa_available(tmp_path):
    workflow.PROBE.clear()
    bv = binaryninja.load(_build(tmp_path), options={workflow.ACTIVITY_SETTING: True})
    try:
        assert workflow.PROBE[_probe_function(bv).start] == {"has_mlil": True, "has_ssa": True}
    finally:
        bv.file.close()
