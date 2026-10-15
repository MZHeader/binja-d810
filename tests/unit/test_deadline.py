"""Deadlines and the shared per-function allowance (spec section 8)."""

import threading
import time

from binja_d810.binja.budget import Ledger
from binja_d810.deadline import Deadline


def test_deadline_basics():
    assert Deadline.never().remaining() > 1e6 and not Deadline.never().expired()
    d = Deadline.after(0.05)
    assert 0 < d.remaining() <= 0.05 and not d.expired()
    time.sleep(0.06)
    assert d.expired() and d.remaining() == 0
    assert Deadline.after(5).earlier(Deadline.after(1)).remaining() <= 1


def test_hlil_pass_gets_only_what_mlil_left():
    ledger = Ledger()
    mlil = ledger.begin(("view", 0x1000), "mlil", generation=111, keepalive=object(), allowance=5.0)
    mlil.charge(3.0)
    hlil = ledger.begin(("view", 0x1000), "hlil", generation=111, keepalive=object(), allowance=5.0)
    assert 1.9 < hlil.remaining() <= 2.0 and hlil.inherited


def test_repeated_calls_in_one_generation_get_no_fresh_allowance():
    ledger = Ledger()
    first = ledger.begin(("view", 0x1000), "mlil", generation=111, keepalive=object(), allowance=5.0)
    first.charge(4.5)
    again = ledger.begin(("view", 0x1000), "mlil", generation=111, keepalive=object(), allowance=5.0)
    assert again.remaining() <= 0.5
    hlil = ledger.begin(("view", 0x1000), "hlil", generation=111, keepalive=object(), allowance=5.0)
    hlil.charge(0.5)
    hlil_again = ledger.begin(("view", 0x1000), "hlil", generation=111, keepalive=object(), allowance=5.0)
    assert hlil_again.remaining() == 0


def test_reanalysis_is_a_new_generation():
    ledger = Ledger()
    ledger.begin(("view", 0x1000), "mlil", generation=111, keepalive=object(), allowance=5.0).charge(5.0)
    fresh = ledger.begin(("view", 0x1000), "mlil", generation=222, keepalive=object(), allowance=5.0)
    assert fresh.remaining() > 4.9


def test_hlil_only_invocation_starts_its_own_generation():
    ledger = Ledger()
    ledger.begin(("view", 0x1000), "mlil", generation=111, keepalive=object(), allowance=5.0).charge(4.0)
    # HLIL regenerated alone (its MLIL is a different object than the one the MLIL pass saw).
    hlil = ledger.begin(("view", 0x1000), "hlil", generation=999, keepalive=object(), allowance=5.0)
    assert hlil.remaining() > 4.9 and not hlil.inherited


def test_views_and_functions_are_separate():
    ledger = Ledger()
    ledger.begin(("view-a", 0x1000), "mlil", generation=1, keepalive=object(), allowance=5.0).charge(5.0)
    assert ledger.begin(("view-b", 0x1000), "mlil", generation=1, keepalive=object(), allowance=5.0).remaining() > 4.9
    assert ledger.begin(("view-a", 0x2000), "mlil", generation=1, keepalive=object(), allowance=5.0).remaining() > 4.9


def test_ledger_is_bounded():
    ledger = Ledger(capacity=3)
    for start in range(10):
        ledger.begin(("view", start), "mlil", generation=start, keepalive=object(), allowance=5.0)
    assert len(ledger) == 3


def test_concurrent_charges_are_counted():
    ledger = Ledger()
    allowance = ledger.begin(("view", 1), "mlil", generation=1, keepalive=object(), allowance=100.0)
    threads = [threading.Thread(target=lambda: [allowance.charge(0.01) for _ in range(100)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert abs(allowance.used() - 4.0) < 1e-6


# --- deadline propagation -------------------------------------------------------------------

from binja_d810.ir.parse import parse  # noqa: E402
from binja_d810.semantic import cobra_shim  # noqa: E402
from binja_d810.semantic.linear_mba import LinearMBABackend  # noqa: E402
from binja_d810.semantic.verify import Check, Verdict, check  # noqa: E402
from binja_d810.simplify import simplify_expression  # noqa: E402


def test_expired_deadline_starts_no_proof():
    a, b = parse("x * (y + 1)", 32), parse("x * y + x", 32)
    assert check(a, b, deadline=Deadline.after(0)) == Check(Verdict.TESTED, "sampling", "proof budget exhausted")


def test_expired_deadline_stops_planning():
    outcome = simplify_expression(parse("(x ^ y) + 2 * (x & y)", 32), [], [LinearMBABackend()],
                                  deadline=Deadline.after(0))
    assert outcome.rewrite is None and outcome.skipped == "budget exhausted"


def test_slow_backend_is_cut_off_by_the_deadline():
    class Slow:
        name = "slow"
        calls = 0

        def simplify(self, e, deadline=None):
            Slow.calls += 1
            time.sleep(0.05)
            return None

    start = time.monotonic()
    simplify_expression(parse("((x ^ y) + 2 * (x & y)) * ((x | y) - (x & y)) + (x ^ (y & z))", 32), [], [Slow()],
                        deadline=Deadline.after(0.12))
    assert time.monotonic() - start < 0.12 + 0.06  # at most one call past the deadline


def test_cobra_lock_wait_respects_the_deadline(monkeypatch):
    import d810_cobra

    class FakeBinding:
        limits = []

        @staticmethod
        def simplify_budgeted(signature, names, bitwidth, tree, max_vars, time_limit_ms, size):
            FakeBinding.limits.append(time_limit_ms)
            return None, False

    monkeypatch.setattr(d810_cobra, "_cobra", FakeBinding, raising=False)
    solve = cobra_shim._python_binding()
    held = threading.Event()
    release = threading.Event()

    def hold():
        with cobra_shim._SOLVE_LOCK:
            held.set()
            release.wait(2)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(1)
    try:
        start = time.monotonic()
        assert solve("(v0 ^ v1) + 2 * (v0 & v1)", 32, Deadline.after(0.1)) is None
        assert time.monotonic() - start < 0.1 + 0.05
        assert cobra_shim.lock_wait_seconds() >= 0.09
    finally:
        release.set()
        holder.join()
    assert solve("(v0 ^ v1) + 2 * (v0 & v1)", 32, Deadline.after(0.2)) is None
    assert FakeBinding.limits and FakeBinding.limits[-1] <= 200  # the solver's own limit is clamped


def test_allowance_stays_inside_binary_ninjas_own_analysis_limit():
    # Binary Ninja abandons a function's analysis after analysis.limits.maxFunctionAnalysisTime
    # (20 s by default): the plugin must never take the function's HLIL down with it.
    from binja_d810.binja.budget import plugin_allowance

    assert plugin_allowance(5, 20_000) == 5
    assert plugin_allowance(60, 20_000) == 10
    assert plugin_allowance(60, 0) == 60  # 0: Binary Ninja imposes no limit
