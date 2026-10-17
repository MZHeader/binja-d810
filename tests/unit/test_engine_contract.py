"""The IL-independent engine against a fake ILAdapter (no Binary Ninja needed)."""

from binja_d810.binja.adapter import Candidate
from binja_d810.binja.engine import EngineConfig, simplify_function
from binja_d810.ir.expr import is_mba, node_count
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


def p(text, width=32, widths=None):
    return parse(text, width, widths)


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

    def shallow(self, candidate, keep=None):
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


def test_roots_that_do_not_simplify_are_not_lifted_again_over_their_shared_values():
    # Ordinary code: most roots with shared definitions do not simplify at all; lifting each again
    # over its shared values doubled planning time on halflife_keygen (median 0.06 -> 0.26 s).
    adapter = SharedAdapter()
    adapter.roots["r"] = p("(x * y) ^ (x + z)")  # no deep rewrite, not too complex
    simplify_function(adapter, [], [LinearMBABackend()], EngineConfig())
    assert adapter.shallow_asked == []


def test_reports_separate_cpu_time_from_waiting():
    # Under contention (many functions' passes in parallel), wall time stretches while the pass's
    # own CPU time does not; the report keeps both so overruns can be told apart from slow work.
    import time

    class Sleepy:
        name = "sleepy"

        def simplify(self, e, deadline=None):
            time.sleep(0.2)  # waiting, not computing
            return None

    adapter = FakeAdapter({"a": p("(x * y) ^ (x + z)")})
    report = simplify_function(adapter, [], [Sleepy()], EngineConfig(max_iterations=1))
    assert report.plan_seconds >= 0.2 and report.plan_cpu_seconds < 0.1


def test_concurrent_passes_are_limited_and_waiting_counts_against_the_deadline():
    # Functions analyzed in parallel contend for Python's lock: on snagit.dll one HLIL pass took 27 s
    # of wall time for about 1 s of work, past Binary Ninja's 20 s limit, and Binary Ninja abandoned
    # such functions. At most PASS_SLOTS passes plan at once; a pass that does not get a slot before
    # its deadline does no work and reports its budget exhausted.
    import threading
    import time

    from binja_d810.binja import engine
    from binja_d810.deadline import Deadline

    running, peak, guard = [0], [0], threading.Lock()

    class Busy:
        name = "busy"

        def simplify(self, e, deadline=None):
            with guard:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with guard:
                running[0] -= 1
            return None

    reports = []

    def work():
        adapter = FakeAdapter({"a": p("(x * y) ^ (x + z)")})
        reports.append(simplify_function(adapter, [], [Busy()], EngineConfig(max_iterations=1), Deadline.after(0.3)))

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] <= engine.PASS_SLOTS
    assert any(r.budget_exhausted and r.plan_seconds == 0 for r in reports)
    assert all(r.total_seconds < 0.3 + 0.15 for r in reports)


def test_reports_say_where_planning_time_goes():
    import time

    class Slow:
        name = "slow"

        def simplify(self, e, deadline=None):
            time.sleep(0.05)
            return None

    adapter = FakeAdapter({"a": p("(x * y) ^ (x + z)"), "b": p("(x * z) ^ (y + z)")})
    report = simplify_function(adapter, [], [Slow()], EngineConfig(max_iterations=1))
    assert report.enumerate_seconds >= 0
    assert len(report.slowest) == 2 and report.slowest[0][1] >= 0.05  # (address, seconds, nodes, outcome)


class SplitAdapter(FakeAdapter):
    """snagit.dll: b = (a & M) | (~a & ~M) over a = x * K + y (single use). Inlining a gives a mixed
    expression where each visible statement was arithmetic or bitwise only; over a alone, b is
    ~(a ^ M)."""

    def __init__(self):
        super().__init__({"b": p("((x * 7 + y) & 0xff00ff00) | (~(x * 7 + y) & 0x00ff00ff)")})
        self.asked: list = []

    def candidates(self, skip):
        if self.roots["b"] == self.original["b"]:
            yield Candidate("b", 0x50, self.roots["b"], visible_nodes=12, visible_mixed=False,
                            consumes=frozenset({"a"}))

    def shallow(self, candidate, keep=None):
        self.asked.append(keep)
        if keep == frozenset({"a"}):
            return Candidate("b", 0x50, p("(a & 0xff00ff00) | (~a & 0x00ff00ff)"), visible_nodes=12,
                             visible_mixed=False)
        return None


def test_a_rewrite_refused_as_newly_mixed_is_tried_over_the_roots_own_inputs():
    from binja_d810.semantic.backend import default_backends

    from binja_d810.rules.registry import load_rules

    adapter = SplitAdapter()
    simplify_function(adapter, load_rules(), default_backends(), EngineConfig())  # a d810 rule does it
    assert frozenset({"a"}) in adapter.asked
    result = adapter.roots["b"]
    assert result != adapter.original["b"] and not is_mba(result) and node_count(result) <= 5, result


class ReuseAdapter(FakeAdapter):
    """x86-64 -O2: `if ((rcx & 1) != 0)` with rcx = (x + 1) * x, x's register reused before the if.
    The version-safe lift keeps rcx; the unchecked lift sees (x + 1) * x and proves the condition
    constant, and a constant reads no variable, so it may replace the condition."""

    def __init__(self):
        super().__init__({"c": p("ne(rcx & 1, 0)", 1, {"rcx": 32})})

    def has_candidates(self):
        return True  # a condition: the real adapters' pre-check accepts comparisons over bitwise ops

    def candidates(self, skip):
        if self.roots["c"] == self.original["c"]:
            yield Candidate("c", 0x60, self.roots["c"],
                            unchecked=p("ne(((x + 1) * x) & 1, 0)", 1, {"x": 32}))


def test_a_condition_proven_constant_through_reused_registers_is_folded():
    from binja_d810.ir.expr import Const
    from binja_d810.semantic.backend import default_backends

    adapter = ReuseAdapter()
    report = simplify_function(adapter, [], default_backends(), EngineConfig())
    assert adapter.roots["c"] == Const(0, 1) and report.rewrites[0].verdict == "PROVEN"


def test_an_unchecked_lift_never_gives_a_non_constant_rewrite():
    from binja_d810.semantic.backend import default_backends

    adapter = ReuseAdapter()
    adapter.roots["c"] = adapter.original["c"] = p("(rcx ^ y) + 2 * (rcx & y)", 32, None)
    adapter.candidates = lambda skip: iter([Candidate("c", 0x60, adapter.roots["c"],
                                                      unchecked=p("((x * 3) ^ y) + 2 * ((x * 3) & y)", 32))])
    simplify_function(adapter, [], default_backends(), EngineConfig(max_iterations=1))
    from binja_d810.ir.expr import variables

    assert {v.name for v in variables(adapter.roots["c"])} <= {"rcx", "y"}  # from the checked lift only


class SharedDagAdapter(FakeAdapter):
    """A condition whose lift, as a tree, is far over maxNodes (snagit.dll's export): the adapter
    offers it as shared values only, and only a proven constant may replace it."""

    def __init__(self, condition):
        super().__init__({"c": condition})

    def has_candidates(self):
        return True

    def candidates(self, skip):
        from binja_d810.ir.expr import Const as C

        if not isinstance(self.roots["c"], C):
            yield Candidate("c", 0x60, self.roots["c"], constant_only=True, shared_dag=self.roots["c"])


def test_a_condition_over_shared_values_is_folded_by_constancy_only():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_constancy import _bcf_condition

    from binja_d810.ir.expr import Const

    adapter = SharedDagAdapter(_bcf_condition())
    report = simplify_function(adapter, [], [LinearMBABackend()], EngineConfig())
    assert adapter.roots["c"] == Const(1, 1), report
    record = report.rewrites[0]
    assert record.verdict == "PROVEN" and "shared" in record.source and len(record.before) < 400, record


class EnclosedAdapter(FakeAdapter):
    """halflife_keygen 0x415890: d = p - ((p + 8) & -8) is its own instruction, used once by
    n + d - 8, which HLIL shows as one arithmetic statement with it. d is -8 | p: correct and
    smaller, but the statement would read n + (-8 | p) - 8, mixed where it was not."""

    def __init__(self, consumer):
        super().__init__({"d": p("p - ((p + 8) & 0xfffffff8)")})
        self.consumer = consumer

    def candidates(self, skip):
        if self.roots["d"] == self.original["d"]:
            yield Candidate("d", 0x70, self.roots["d"], visible_nodes=7, visible_mixed=True,
                            consumer_kinds=self.consumer, root_kinds=frozenset({"arith"}))  # q is its own statement


def test_a_rewrite_never_makes_the_statement_that_uses_it_mixed():
    from binja_d810.rules.registry import load_rules
    from binja_d810.semantic.backend import default_backends

    refused = EnclosedAdapter(frozenset({"arith"}))
    report = simplify_function(refused, load_rules(), default_backends(), EngineConfig())
    assert refused.roots["d"] == refused.original["d"], report.rewrites
    assert any("newly mixed" in s.reason for s in report.skipped), report.skipped
    allowed = EnclosedAdapter(frozenset({"bitwise"}))  # a bitwise user: -8 | p reads fine there
    simplify_function(allowed, load_rules(), default_backends(), EngineConfig())
    assert allowed.roots["d"] != allowed.original["d"]


class PathAdapter(FakeAdapter):
    """x-tunnel.bin: 2 / (x * x + 1) == i with i merged from y1 * y1 + 3 and y2 * y2 + 3: not constant
    over i, false on each incoming path. The adapter offers the per-path variants."""

    def __init__(self, variants):
        super().__init__({"c": p("eq(udiv(2, (x * x) + 1), i)", 1, {"x": 32, "i": 32})})
        self.variants = variants

    def has_candidates(self):
        return True

    def candidates(self, skip):
        if self.roots["c"] == self.original["c"]:
            yield Candidate("c", 0x80, self.roots["c"])

    def path_variants(self, candidate):
        return [self.variants]  # alternatives, cheapest first: one here


def test_a_condition_constant_on_every_incoming_path_is_folded():
    from binja_d810.ir.expr import Const
    from binja_d810.semantic.backend import default_backends

    widths = {"x": 32, "y": 32, "z": 32}
    agree = PathAdapter([p("eq(udiv(2, (x * x) + 1), (y * y) + 3)", 1, widths),
                         p("eq(udiv(2, (x * x) + 1), (z * z) + 3)", 1, widths)])
    report = simplify_function(agree, [], default_backends(), EngineConfig())
    assert agree.roots["c"] == Const(0, 1), report
    assert report.rewrites[0].source == "constancy (per path)" and report.rewrites[0].verdict == "PROVEN"
    for variants in ([p("eq(udiv(2, (x * x) + 1), (y * y) + 3)", 1, widths), p("eq(udiv(2, (x * x) + 1), z)", 1, widths)],
                     [p("eq(x, x)", 1, widths), p("ne(y, y)", 1, widths)]):  # one path varies; two constants
        refused = PathAdapter(variants)
        simplify_function(refused, [], default_backends(), EngineConfig())
        assert refused.roots["c"] == refused.original["c"], variants


def test_a_condition_offered_with_its_paths_is_folded_from_them():
    # A condition over merged values that is no candidate itself (k != 0, k = phi(...)): the adapter
    # offers it constant-only with the per-path variants it computed.
    from binja_d810.ir.expr import Const
    from binja_d810.semantic.backend import default_backends

    widths = {"x": 32, "y": 32, "k": 32}
    adapter = PathAdapter(None)
    adapter.roots["c"] = adapter.original["c"] = p("ne(k, 0)", 1, widths)
    paths = (p("ne(((x - 1) * x) & 1, 0)", 1, widths), p("ne((y + (y * y)) & 1, 0)", 1, widths))
    adapter.candidates = lambda skip: iter([Candidate("c", 0x90, adapter.roots["c"], constant_only=True, paths=(paths,))]
                                           if adapter.roots["c"] == adapter.original["c"] else [])
    report = simplify_function(adapter, [], default_backends(), EngineConfig())
    assert adapter.roots["c"] == Const(0, 1), report


def test_path_alternatives_are_tried_in_order():
    # v3.3: invariant merges substituted alone first (one variant, the other merges free), then the
    # full split. Either may fold; a set folds only if all its variants are the same proven constant.
    from binja_d810.ir.expr import Const
    from binja_d810.semantic.backend import default_backends

    widths = {"x": 32, "y": 32, "z": 32}
    never = p("eq(udiv(2, (x * x) + 1), (y * y) + 3)", 1, widths)
    varies = p("eq(udiv(2, (x * x) + 1), z)", 1, widths)
    for alternatives, folds in (([[never], [varies]], True), ([[varies], [never, never]], True),
                                ([[varies], [never, varies]], False)):
        adapter = PathAdapter(None)
        adapter.path_variants = lambda candidate, alternatives=alternatives: alternatives
        simplify_function(adapter, [], default_backends(), EngineConfig())
        assert (adapter.roots["c"] == Const(0, 1)) == folds, alternatives


def test_per_path_constancy_waits_for_every_other_candidate():
    # v3.3: per-path folding is the costliest mechanism and the rarest; it runs after the last round's
    # other attempts on every candidate, with what remains of the budget (snagit.dll's 271 shared-value
    # folds came after per-path attempts on earlier conditions and ran out of time).
    from binja_d810.ir.expr import Const
    from binja_d810.semantic.backend import default_backends

    events = []
    widths = {"x": 32, "y": 32, "z": 32, "i": 32}
    adapter = PathAdapter([p("eq(udiv(2, (x * x) + 1), (y * y) + 3)", 1, widths)])
    adapter.roots["m"] = adapter.original["m"] = p("(x ^ y) + 2 * (x & y)")
    first = Candidate("c", 0x80, adapter.roots["c"])

    def candidates(skip):
        if adapter.roots["c"] == adapter.original["c"]:
            yield first  # the condition comes first, as the adapter's last-first order may put it
        if adapter.roots["m"] == adapter.original["m"]:
            yield Candidate("m", 0x84, adapter.roots["m"])

    def path_variants(candidate):
        events.append("paths")
        return [adapter.variants]

    real_replace = adapter.replace
    adapter.candidates, adapter.path_variants = candidates, path_variants
    adapter.replace = lambda candidate, replacement: (events.append(f"replace {candidate.root}"),
                                                      real_replace(candidate, replacement))
    backend = default_backends()[0]
    real_simplify = backend.simplify
    backend.simplify = lambda e, **kw: (events.append("tier2"), real_simplify(e, **kw))[1]
    simplify_function(adapter, [], [backend], EngineConfig(max_iterations=1))
    assert adapter.roots["c"] == Const(0, 1) and adapter.roots["m"] != adapter.original["m"]
    assert events.index("paths") > max(i for i, e in enumerate(events) if e == "tier2"), events


def test_constant_folds_come_before_every_backend():
    # snagit.dll's sub_180009da0 (v3.3): hundreds of junk MBA roots took the whole budget in Tier 2
    # before the opaque predicates' constant-only folds (shared values, 10-60 ms each) were reached.
    # Constant folds run first in each pass: a folded predicate removes the junk behind it.
    import sys
    import time
    from pathlib import Path

    from binja_d810.deadline import Deadline
    from binja_d810.ir.expr import Const

    sys.path.insert(0, str(Path(__file__).parent))
    from test_constancy import _bcf_condition

    class Slow:
        name = "slow"

        def simplify(self, e, deadline=None):
            time.sleep(0.2)
            return None

    roots = {f"m{k}": p(f"(x{k} ^ y) + 2 * (x{k} & y)") for k in range(10)}
    roots["c"] = _bcf_condition()
    adapter = FakeAdapter(roots)
    adapter.has_candidates = lambda: True

    def candidates(skip):
        for name, e in adapter.roots.items():
            if name == "c" and not isinstance(e, Const):
                yield Candidate("c", 0x60, e, constant_only=True, shared_dag=e)  # last, as the order may put it
            elif name != "c" and adapter.roots[name] == adapter.original[name]:
                yield Candidate(name, 0x10, e)

    adapter.candidates = candidates
    simplify_function(adapter, [], [Slow()], EngineConfig(max_iterations=1), Deadline.after(1.0))
    assert adapter.roots["c"] == Const(1, 1)


def test_a_pass_that_folds_constants_commits_them_before_any_backend_runs():
    # v3.3: folded predicates remove the code behind them, so the backends wait for the next pass, over
    # what remains (snagit.dll's sub_180009da0: Tier 2 on junk in branches that were about to disappear).
    import sys
    from pathlib import Path

    from binja_d810.ir.expr import Const

    sys.path.insert(0, str(Path(__file__).parent))
    from test_constancy import _bcf_condition

    calls = []

    class Recording:
        name = "recording"

        def simplify(self, e, deadline=None):
            calls.append(adapter.roots["c"])
            return None

    adapter = FakeAdapter({"c": _bcf_condition(), "m": p("(x ^ y) + 2 * (x & y) + (x | y)")})
    adapter.has_candidates = lambda: True

    def candidates(skip):
        if not isinstance(adapter.roots["c"], Const):
            yield Candidate("c", 0x60, adapter.roots["c"], constant_only=True, shared_dag=adapter.roots["c"])
        yield Candidate("m", 0x10, adapter.roots["m"])

    adapter.candidates = candidates
    report = simplify_function(adapter, [], [Recording()], EngineConfig())
    assert adapter.roots["c"] == Const(1, 1), report
    assert calls and all(root == Const(1, 1) for root in calls), "a backend ran before the fold was committed"


def test_a_stuck_candidate_still_gets_its_constant_only_attempts():
    # The cache of stuck candidates is keyed by the version-safe lift, but the per-path variants and the
    # constant-only lifts depend on more (x-tunnel.bin 0x4485a0: a merge that resolves only once other
    # folds changed the IL). Only the simplification is skipped; those attempts run again.
    from binja_d810.ir.expr import Const
    from binja_d810.semantic.backend import default_backends

    widths = {"x": 32, "y": 32, "z": 32}
    other = p("(z ^ y) + 2 * (z & y)")
    adapter = PathAdapter([p("eq(udiv(2, (x * x) + 1), z)", 1, widths)])  # not constant in the first pass
    adapter.roots["m"] = adapter.original["m"] = other
    real_candidates = adapter.candidates

    def candidates(skip):
        yield from real_candidates(skip)
        if adapter.roots["m"] == adapter.original["m"]:
            yield Candidate("m", 0x90, adapter.roots["m"])  # its rewrite gives a second pass

    def replace(candidate, replacement):
        adapter.roots[candidate.root] = replacement
        adapter.variants = [p("eq(udiv(2, (x * x) + 1), (y * y) + 3)", 1, widths)]  # the IL around changed

    adapter.candidates, adapter.replace = candidates, replace
    simplify_function(adapter, [], default_backends(), EngineConfig())
    assert adapter.roots["c"] == Const(0, 1)
