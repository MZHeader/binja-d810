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


class ChainAdapter(FakeAdapter):
    """t1 = MBA (single use, inlined by r); r = t1 + MBA."""

    def __init__(self, consumer_simplifies=True):
        super().__init__({"t1": p("(x ^ y) + 2 * (x & y)"),
                          "r": p("((x ^ y) + 2 * (x & y)) + ((x | y) - (x & y))") if consumer_simplifies else p("z")})
        self.planned: list = []

    def candidates(self, skip):  # last first
        yield Candidate("r", 0x20, self.roots["r"], consumes=frozenset({"t1"}))
        yield Candidate("t1", 0x10, self.roots["t1"], defines="t1", single_use=True)


def test_single_use_definitions_inlined_by_a_planned_candidate_are_not_simplified_twice():
    adapter = ChainAdapter()
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig(max_iterations=1))
    assert [r.address for r in report.rewrites] == [0x20] and report.subsumed == 1


def test_a_consumer_that_does_not_simplify_leaves_its_inputs_to_be_simplified():
    adapter = ChainAdapter(consumer_simplifies=False)
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig(max_iterations=1))
    assert [r.address for r in report.rewrites] == [0x10] and report.subsumed == 0


class VisibleAdapter(FakeAdapter):
    """One root whose lift inlined definitions that stay alive (visible form: 5 nodes, unmixed)."""

    def __init__(self, visible_nodes, visible_mixed):
        super().__init__({"r": p("(b - a) + (a & 0xffffff80)")})
        self.visible = (visible_nodes, visible_mixed)

    def candidates(self, skip):
        yield Candidate("r", 0x30, self.roots["r"], visible_nodes=self.visible[0], visible_mixed=self.visible[1])


def test_a_rewrite_never_introduces_a_mix_the_visible_code_did_not_have():
    # strcasecmp: rsi_2 = arg2 - arg1 + rdi_1 with rdi_1 = arg1 & ~0x7f still used elsewhere. The lift
    # is mixed and shrinks to arg2 - (0x7f & arg1), but what the user sees was unmixed.
    from binja_d810.semantic.backend import default_backends

    adapter = VisibleAdapter(visible_nodes=5, visible_mixed=False)
    report = simplify_function(adapter, [], default_backends(), EngineConfig())
    assert report.rewrites == [] and any("visible" in s.reason for s in report.skipped)


def test_a_rewrite_that_shrinks_the_visible_code_is_applied():
    from binja_d810.semantic.backend import default_backends

    adapter = VisibleAdapter(visible_nodes=9, visible_mixed=True)
    report = simplify_function(adapter, [], default_backends(), EngineConfig())
    assert len(report.rewrites) == 1


class CountingBackend:
    """LinearMBABackend that counts the expressions it is asked to simplify."""

    name = "counting"

    def __init__(self):
        from collections import Counter

        self.inner = LinearMBABackend()
        self.calls: Counter = Counter()

    def simplify(self, e, deadline=None):
        self.calls[e] += 1
        return self.inner.simplify(e, deadline=deadline)


def test_an_unchanged_candidate_that_did_not_simplify_is_not_planned_again():
    # Iteration 1 rewrites "b", so the engine runs a second iteration; "a" is the same expression
    # then, and its outcome (no rewrite) is a function of that expression alone.
    stuck = p("(x * y) ^ (x + z)")
    adapter = FakeAdapter({"a": stuck, "b": p("(x ^ y) + 2 * (x & y)")})
    backend = CountingBackend()
    report = simplify_function(adapter, [], [backend], EngineConfig())
    assert [r.address for r in report.rewrites] == [0x1000]
    assert backend.calls[stuck] == 1


def test_cheap_backends_reach_every_candidate_before_slow_ones_spend_the_budget():
    # tigress_vm: one function with 248 candidates. CoBRA's slow failures on the first candidates
    # left no budget for later ones the linear backend answers in milliseconds.
    import time

    from binja_d810.deadline import Deadline

    class Slow:
        name = "slow"

        def simplify(self, e, deadline=None):
            time.sleep(0.2)
            return None

    roots = {"n1": p("(x * y) ^ (x + z)"), "n2": p("(y * z) ^ (y + x)")}
    roots.update({f"l{i}": p(f"(x ^ {i}) + 2 * (x & {i})") for i in range(1, 4)})
    adapter = FakeAdapter(roots)
    report = simplify_function(adapter, [], [LinearMBABackend(), Slow()], EngineConfig(), Deadline.after(0.3))
    assert {r for r, e in adapter.roots.items() if e != roots[r]} == {"l1", "l2", "l3"}
    assert report.budget_exhausted


class SharedAdapter(FakeAdapter):
    """r = (a ^ z) + 2 * (a & z) where a = (x * y) - (x | y) + (x & 7) is also used elsewhere: the
    deep lift inlines a, and its result a + z copies a's definition into r (tigress_vm)."""

    def __init__(self):
        super().__init__({"r": p("((((x * y) - (x | y)) + (x & 7)) ^ z) + 2 * ((((x * y) - (x | y)) + (x & 7)) & z)")})
        self.shallow_asked = []

    def candidates(self, skip):
        if is_mba(self.roots["r"]):  # as the real adapters: only MBA roots are candidates
            yield Candidate("r", 0x40, self.roots["r"], visible_nodes=7, visible_mixed=True, shared=frozenset({"a"}))

    def shallow(self, candidate):
        self.shallow_asked.append(candidate.root)
        return Candidate("r", 0x40, p("(a ^ z) + 2 * (a & z)"), visible_nodes=7, visible_mixed=True)


def test_a_root_whose_deep_lift_grows_the_visible_code_is_simplified_over_its_shared_values():
    from binja_d810.semantic.backend import default_backends

    adapter = SharedAdapter()
    report = simplify_function(adapter, [], default_backends(), EngineConfig())
    assert adapter.shallow_asked == ["r"]
    assert adapter.roots["r"] == p("a + z") or adapter.roots["r"] == p("z + a")
    assert len(report.rewrites) == 1 and not any("visible" in s.reason for s in report.skipped)


def test_a_smaller_result_over_shared_values_wins_even_when_the_deep_one_shrinks():
    # i686 -O2: the deep rewrite shrank the root (11 -> 9 nodes) but copied a's definition.
    from binja_d810.semantic.backend import default_backends

    adapter = SharedAdapter()
    adapter.candidates = lambda skip: iter([Candidate("r", 0x40, adapter.roots["r"], visible_nodes=40,
                                                      visible_mixed=True, shared=frozenset({"a"}))])
    simplify_function(adapter, [], default_backends(), EngineConfig(max_iterations=1))
    assert adapter.roots["r"] in (p("a + z"), p("z + a"))
