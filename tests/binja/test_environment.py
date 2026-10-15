import pytest
from binaryninja import Workflow

from binja_d810.binja import workflow


def test_activity_registered_on_meta_analysis():
    assert Workflow("core.function.metaAnalysis").contains(workflow.ACTIVITY_NAME)


def test_activity_registered_on_objective_c_when_present():
    if "core.function.objectiveC" not in Workflow:
        pytest.skip("this build has no core.function.objectiveC workflow")
    assert Workflow("core.function.objectiveC").contains(workflow.ACTIVITY_NAME)


def test_auto_setting_identifier():
    keys = Workflow("core.function.metaAnalysis").eligibility_settings()
    assert workflow.ACTIVITY_SETTING in keys, keys


def test_plugin_under_test_is_the_requested_one():
    import os
    from pathlib import Path

    import binja_d810

    requested = os.environ.get("BINJA_D810_PLUGIN")
    if not requested:
        pytest.skip("BINJA_D810_PLUGIN not set: the installed plugin is under test")
    assert Path(binja_d810.__file__).resolve().parent == Path(requested).resolve()


def test_hlil_activity_registered_after_hlil_generation():
    wf = Workflow("core.function.metaAnalysis")
    assert wf.contains(workflow.HLIL_ACTIVITY_NAME)
    # No eligibility setting of its own: the MLIL activity's setting is the one toggle.
    assert workflow.HLIL_ACTIVITY_NAME not in wf.eligibility_settings()
