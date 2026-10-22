"""Completion means native analysis and the deferred flow queue have both finished."""

from types import SimpleNamespace

import pytest
from binaryninja import AnalysisState

from binja_d810.binja import completion
from binja_d810.binja.flow import apply


class View:
    def __init__(self, key):
        self.file = SimpleNamespace(session_id=key, filename=f"/fixtures/sample-{key}")
        self.analysis_info = SimpleNamespace(state=AnalysisState.IdleState)
        self.callbacks = []
        self.flow_busy = False

    def add_analysis_completion_event(self, callback):
        self.callbacks.append(callback)
        return callback

    def finish(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


@pytest.fixture
def messages(monkeypatch):
    result = []
    monkeypatch.setattr(completion, "_PENDING", {})
    monkeypatch.setattr(completion.log, "logger", SimpleNamespace(log_info=result.append))
    monkeypatch.setattr(apply, "busy", lambda view: view.flow_busy)
    return result


def test_many_passes_produce_one_completion(messages):
    view = View(1)
    for _ in range(20):
        completion.touch(view)
    assert len(view.callbacks) == 1
    view.finish()
    completion.done(view)
    assert messages == ["D810: analysis complete for sample-1."]
    assert not completion._PENDING


def test_waits_for_deferred_flow_and_undo(messages):
    view = View(1)
    view.flow_busy = True
    completion.touch(view)
    view.finish()
    assert not messages
    view.flow_busy = False
    completion.done(view)
    assert len(messages) == 1


def test_menu_batch_holds_completion(messages):
    view = View(1)
    with completion.batch(view):
        completion.touch(view)
        completion.done(view)
        assert not messages and not view.callbacks
    view.finish()
    assert len(messages) == 1


def test_native_analysis_starting_again_rearms_event(messages):
    view = View(1)
    completion.touch(view)
    view.analysis_info.state = AnalysisState.AnalyzeState
    view.finish()
    assert not messages and len(view.callbacks) == 1
    view.analysis_info.state = AnalysisState.IdleState
    view.finish()
    assert len(messages) == 1


def test_views_and_error_results_are_independent(messages):
    first, second = View(1), View(2)
    completion.touch(first)
    completion.touch(second)
    completion.failed(second)
    second.finish()
    first.finish()
    assert messages == ["D810: analysis finished with errors for sample-2.",
                        "D810: analysis complete for sample-1."]


def test_old_callback_cannot_complete_a_new_batch(messages):
    view = View(1)
    completion.touch(view)
    old = view.callbacks[0]
    view.finish()
    completion.touch(view)
    old()
    assert len(messages) == 1
    view.finish()
    assert len(messages) == 2


def test_batch_failure_is_not_announced_as_success(messages):
    view = View(1)
    with pytest.raises(ValueError), completion.batch(view):
        raise ValueError("test")
    view.finish()
    assert "finished with errors" in messages[0]
