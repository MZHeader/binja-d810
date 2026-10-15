"""The IL-independent engine against a fake ILAdapter (no Binary Ninja needed)."""

from binja_d810.binja.adapter import Candidate
from binja_d810.binja.engine import EngineConfig, simplify_function
from binja_d810.ir.expr import is_mba
from binja_d810.ir.parse import parse
from binja_d810.semantic.linear_mba import LinearMBABackend


class FakeAdapter:
    """A 'function' of named roots holding IR; replacements write into it."""

    level = "fake"

    def __init__(self, roots, dead_before=(), dead_after=(), change_cfg=False, fail_on_replace=None):
        self.roots = dict(roots)
        self.original = dict(roots)
        self.dead = set(dead_before)
        self.dead_after = set(dead_after)
        self.removed: set = set()
        self.finished = 0
        self.restored = False
        self.change_cfg = change_cfg
        self.fail_on_replace = fail_on_replace
        self.shape = ("blocks", 1)

    def has_candidates(self):
        return any(is_mba(e) for e in self.roots.values())

    def candidates(self, skip):
        skip(0x99, "effect: MLIL_LOAD_SSA")
        for name, e in self.roots.items():
            if is_mba(e):
                yield Candidate(name, 0x1000, e)

    def replace(self, candidate, replacement):
        if candidate.root == self.fail_on_replace:
            raise RuntimeError("injected")
        self.roots[candidate.root] = replacement
        if self.change_cfg:
            self.shape = ("blocks", 2)

    def finish(self):
        self.finished += 1
        self.dead |= self.dead_after

    def dead_assignments(self):
        return set(self.dead) - self.removed

    def remove(self, dead):
        self.removed |= dead

    def cfg_shape(self):
        return self.shape

    def restore(self):
        self.roots = dict(self.original)
        self.removed.clear()
        self.restored = True


def p(text):
    return parse(text, 32)


def test_rewrites_are_applied_and_reported_with_level():
    adapter = FakeAdapter({"r": p("(x ^ y) + 2 * (x & y)")})
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig())
    assert adapter.roots["r"] == p("x + y")
    assert [(r.level, r.verdict, r.method) for r in report.rewrites] == [("fake", "PROVEN", "signature")]
    assert [(s.address, s.reason, s.level) for s in report.skipped][:1] == [(0x99, "effect: MLIL_LOAD_SSA", "fake")]


def test_only_newly_dead_assignments_are_removed():
    adapter = FakeAdapter({"r": p("(x ^ y) + 2 * (x & y)")}, dead_before={"z_1"}, dead_after={"t0", "t1"})
    simplify_function(adapter, [], [LinearMBABackend()], EngineConfig())
    assert adapter.removed == {"t0", "t1"}


def test_control_flow_change_rolls_back():
    adapter = FakeAdapter({"r": p("(x ^ y) + 2 * (x & y)")}, change_cfg=True)
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig())
    assert adapter.restored and adapter.roots["r"] == p("(x ^ y) + 2 * (x & y)")
    assert report.rolled_back and report.rewrites == [] and report.rolled_back_rewrites == 1
    assert report.errors[-1].source == "engine" and "control flow" in report.errors[-1].message


def test_failure_mid_replacement_rolls_back_everything():
    adapter = FakeAdapter({"a": p("(x ^ y) + 2 * (x & y)"), "b": p("(x | y) - (x & y)")}, fail_on_replace="b")
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig())
    assert adapter.restored and adapter.roots == adapter.original
    assert report.rolled_back and not report.rewrites


def test_function_without_mixed_operations_is_untouched():
    adapter = FakeAdapter({"r": p("x + y")})
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig())
    assert adapter.finished == 0 and report.rewrites == [] and report.skipped == []


def test_iteration_cap_is_reported():
    class Endless(FakeAdapter):
        def replace(self, candidate, replacement):
            super().replace(candidate, p("(x ^ y) + 2 * (x & y)"))  # never converges

    adapter = Endless({"r": p("(x ^ y) + 2 * (x & y)")})
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig(max_iterations=3))
    assert report.capped and report.still_firing == ["tier2:linear"] and len(report.rewrites) == 3


def test_expired_allowance_does_nothing():
    from binja_d810.deadline import Deadline

    adapter = FakeAdapter({"r": p("(x ^ y) + 2 * (x & y)")})
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig(), Deadline.after(0))
    assert report.budget_exhausted and adapter.roots["r"] == p("(x ^ y) + 2 * (x & y)") and adapter.finished == 0


def test_exhaustion_mid_planning_still_applies_verified_candidates():
    import time

    from binja_d810.deadline import Deadline

    class SlowLinear(LinearMBABackend):
        def simplify(self, e, deadline=None):
            time.sleep(0.08)
            return super().simplify(e, deadline)

    roots = {name: p(f"({name} ^ y) + 2 * ({name} & y)") for name in ("a", "b", "c", "d", "e")}
    adapter = FakeAdapter(roots)
    report = simplify_function(adapter, [], [SlowLinear()], EngineConfig(), Deadline.after(0.12))
    applied = {r for r, e in adapter.roots.items() if e != roots[r]}
    assert report.budget_exhausted and 1 <= len(applied) < len(roots)
    assert len(report.rewrites) == len(applied) and not report.rolled_back
    assert report.plan_seconds >= 0.12 and report.overrun_seconds < 0.2
