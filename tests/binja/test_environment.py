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
