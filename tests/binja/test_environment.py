import pytest
from binaryninja import Workflow

from binja_d810.binja import workflow


def test_activity_registered_on_meta_analysis():
    assert Workflow("core.function.metaAnalysis").contains(workflow.ACTIVITY_NAME)


def test_activity_registered_on_objective_c_when_present():
    if "core.function.objectiveC" not in {wf.name for wf in Workflow}:
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


def test_unflattening_runs_after_indirect_branches_are_resolved():
    # That analysis reads ctx.mlil: unflattening before it removes the jump it resolves (v4.2).
    wf = Workflow("core.function.metaAnalysis")
    stages = [s for s in map(wf.subactivities, wf.subactivities("", immediate=False)) if workflow.ACTIVITY_NAME in s]
    assert stages
    for order in stages:  # each analysis mode that runs the MLIL pass
        assert order.index(workflow.ACTIVITY_NAME) < order.index(workflow.UNFLATTEN_AFTER) \
            < order.index(workflow.UNFLATTEN_ACTIVITY_NAME), order
    # No eligibility setting of its own: the MLIL activity's setting is the one toggle.
    assert workflow.UNFLATTEN_ACTIVITY_NAME not in wf.eligibility_settings()


def test_unflattening_registered_on_objective_c_when_present():
    if "core.function.objectiveC" not in {wf.name for wf in Workflow}:
        pytest.skip("this build has no core.function.objectiveC workflow")
    assert Workflow("core.function.objectiveC").contains(workflow.UNFLATTEN_ACTIVITY_NAME)


def test_optional_workflow_lookup_does_not_create_an_empty_workflow(monkeypatch):
    before={wf.name for wf in Workflow}
    absent='tests.d810.absent.optional.workflow'
    assert absent not in before
    monkeypatch.setattr(workflow,'BASE_WORKFLOWS',(absent,))
    assert workflow.register()==[]
    assert {wf.name for wf in Workflow}==before


def test_registered_plugin_activities_are_connected_to_native_entry_points():
    for wf in Workflow:
        if wf.name not in workflow.BASE_WORKFLOWS:continue
        assert wf.contains('core.function.generateMediumLevelIL')
        assert not any(name.startswith('extension.binja_d810.') for name in wf.activity_roots())


def test_failed_insertion_keeps_the_registered_native_workflow(monkeypatch):
    name='core.function.metaAnalysis'
    before=Workflow(name).configuration()
    monkeypatch.setattr(workflow,'BASE_WORKFLOWS',(name,))
    monkeypatch.setattr(Workflow,'insert_after',lambda *args,**kwargs:False)
    assert workflow.register()==[]
    assert Workflow(name).configuration()==before
