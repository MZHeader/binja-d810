"""Queue batches must not regenerate native IL unless something actually changed."""

from types import SimpleNamespace

import pytest
from binaryninja import AnalysisState

from binja_d810.binja.flow import apply
from binja_d810.flow.record import Change, Record


class View:
    """Record native analysis requests and deliver completion callbacks after each update."""

    def __init__(self):
        self.events, self.trace = [], []
        self.generation = 0
        self.analysis_info = SimpleNamespace(state=AnalysisState.IdleState)

    def get_function_at(self, start):
        return SimpleNamespace(reanalyze=lambda: self.trace.append(("reanalyze", start, self.generation)))

    def update_analysis(self):
        self.trace.append(("update", self.generation))

    def update_analysis_and_wait(self):
        self.generation += 1
        self.analysis_info.state = AnalysisState.IdleState
        self.trace.append(("wait", self.generation))
        events, self.events = self.events, []
        for event in events:
            event()

    def add_analysis_completion_event(self, callback):
        self.events.append(callback)
        return callback


@pytest.fixture
def queue(monkeypatch):
    view, state = View(), apply.State(Record(), running=True)
    monkeypatch.setattr(apply, "state_of", lambda _: state)
    monkeypatch.setattr(apply, "_store", lambda *_: None)
    monkeypatch.setattr(apply, "SITES_PER_ROUND", 2)
    return view, state


def _sites(state, count):
    for n in range(count):
        site = SimpleNamespace(key=n, function=0x1000 + n * 0x10)
        state.pending[site.key] = site
        state.enabled.add(site.function)


def _finished(view, state):
    assert not state.pending
    assert not state.running and not state.armed and not state.again
    assert not view.events


def test_rejected_batches_drain_without_native_reanalysis(queue, monkeypatch):
    view, state = queue
    _sites(state, 7)
    seen = []
    monkeypatch.setattr(apply, "decide", lambda work, site: seen.append(site.key))
    apply._run_completed(view, state)
    assert seen == list(range(7))
    assert not view.trace
    _finished(view, state)


def test_a_change_refreshes_once_before_deciding_the_remaining_batch(queue, monkeypatch):
    view, state = queue
    _sites(state, 7)
    seen = []

    def decide(work, site):
        seen.append((site.key, view.generation))
        if site.key == 3:
            work.touched.add(site.function)

    monkeypatch.setattr(apply, "decide", decide)
    apply._run_completed(view, state)
    assert seen == [(n, int(n >= 4)) for n in range(7)]
    assert view.trace == [("reanalyze", 0x1030, 0), ("update", 0), ("wait", 1)]
    _finished(view, state)


def test_expired_batch_time_does_not_force_a_function_refresh(queue, monkeypatch):
    view, state = queue
    _sites(state, 7)
    seen = []

    class Deadline:
        @staticmethod
        def after(_):
            count = len(seen)
            return SimpleNamespace(expired=lambda: len(seen) > count)

    monkeypatch.setattr(apply, "Deadline", Deadline)
    monkeypatch.setattr(apply, "decide", lambda work, site: seen.append(site.key))
    apply._run_completed(view, state)
    assert seen == list(range(7))
    assert not view.trace
    _finished(view, state)


def test_readers_refresh_only_after_their_changed_callee_is_analyzed(queue, monkeypatch):
    view, state = queue
    _sites(state, 1)
    state.readers[0x2000].add(0x3000)
    monkeypatch.setattr(apply, "decide", lambda work, site: work.touched.add(0x2000))
    apply._run_completed(view, state)
    assert view.trace == [("reanalyze", 0x2000, 0), ("update", 0), ("wait", 1),
                          ("reanalyze", 0x3000, 1), ("update", 1), ("wait", 2)]
    _finished(view, state)


def test_signature_undo_waits_for_the_original_body(queue, monkeypatch):
    view, state = queue
    state.record.add(Change("branches", 0x1010, {"function": 0x1000}))
    state.record.add(Change("signature", 0x1000, {"type": "int f(void)"}))
    reverted = []

    def revert(view, change):
        reverted.append((change.kind, view.generation))
        return {0x1000}

    monkeypatch.setattr(apply.undo, "revert", revert)
    apply._run_completed(view, state)
    assert reverted == [("branches", 0), ("signature", 1)]
    assert view.generation == 2
    assert not len(state.record)
    _finished(view, state)


def test_work_enqueued_during_a_rejected_batch_is_not_lost(queue, monkeypatch):
    view, state = queue
    _sites(state, 1)
    seen = []

    def decide(work, site):
        seen.append(site.key)
        if site.key == 0:
            apply.enqueue(view, SimpleNamespace(key=1, function=site.function))

    monkeypatch.setattr(apply, "decide", decide)
    apply._run_completed(view, state)
    assert seen == [0, 1]
    assert not view.trace
    _finished(view, state)


def test_external_analysis_finishes_before_the_next_batch(queue, monkeypatch):
    view, state = queue
    _sites(state, 1)
    seen = []

    def decide(work, site):
        seen.append((site.key, view.generation))
        if site.key == 0:
            view.analysis_info.state = AnalysisState.AnalyzeState
            apply.enqueue(view, SimpleNamespace(key=1, function=site.function))

    monkeypatch.setattr(apply, "decide", decide)
    apply._run_completed(view, state)
    assert seen == [(0, 0), (1, 1)]
    assert view.trace == [("wait", 1)]
    _finished(view, state)
