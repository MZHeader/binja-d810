"""Prove each vendored d810 rule with Z3 at 8/16/32/64 bits; write binja_d810/rules/verified_rules.json.

Usage: uv run python scripts/verify_rules.py [--timeout 60] [--jobs N]
       uv run python scripts/verify_rules.py --reprove [--only NAME ...] [--timeout 600] [--tactic bitblast]

--reprove keeps the manifest and re-checks every rule-width d810's verifier did not prove (refuted,
timed out, error) with binja_d810.rules.prove, which converts constraints at the proof's width
(d810's converter assumes 32 bits). A width is enabled only if proven; a timeout stays recorded.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "binja_d810" / "rules" / "verified_rules.json"
WIDTHS = (8, 16, 32, 64)
SOURCE = {"repo": "w00tzenheimer/d810-ng", "sha": "2e59ab4ce18e5e82911880137d1285d95279981a"}


def _rules() -> dict:
    from binja_d810.third_party.d810.mba.rules import VerifiableRule

    return {type(r).__name__: r for r in VerifiableRule.instantiate_all()}


def _prove(name: str, width: int, queue) -> None:
    try:
        from binja_d810.third_party.d810.mba.backends.z3 import verify_rule

        rule = _rules()[name]
        rule.SKIP_VERIFICATION = False
        rule.BIT_WIDTH = width
        queue.put("proven" if verify_rule(rule, bit_width=width) else "refuted")
    except AssertionError as exc:
        first = str(exc).splitlines()[0][:200] if str(exc) else ""
        queue.put(f"refuted: {first}")
    except Exception as exc:
        queue.put(f"error: {type(exc).__name__}: {str(exc)[:200]}")


def _run_job(name: str, width: int, timeout: float) -> str:
    queue = mp.Queue()
    proc = mp.Process(target=_prove, args=(name, width, queue))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        return f"timeout after {timeout:g}s"
    return queue.get() if not queue.empty() else f"crashed (exit {proc.exitcode})"


def _reprove_job(name: str, width: int, timeout: float, tactic, queue) -> None:
    try:
        from binja_d810.rules.prove import prove_rule

        queue.put(prove_rule(_rules()[name], width, timeout_ms=int(timeout * 1000), tactic=tactic))
    except Exception as exc:
        queue.put(f"error: {type(exc).__name__}: {str(exc)[:200]}")


def _run_reprove(name: str, width: int, timeout: float, tactic) -> str:
    queue = mp.Queue()
    proc = mp.Process(target=_reprove_job, args=(name, width, timeout, tactic, queue))
    proc.start()
    proc.join(timeout + 30)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        return f"timeout after {timeout:g}s"
    return queue.get() if not queue.empty() else f"crashed (exit {proc.exitcode})"


def reprove(args) -> None:
    data = json.loads(OUT.read_text())
    entries = data["rules"]
    jobs = []
    for name, entry in sorted(entries.items()):
        if args.only and name not in args.only:
            continue
        if any(n.startswith(("excluded", "deferred")) for n in entry["notes"]):
            continue
        for width in WIDTHS:
            if width not in entry["widths"]:
                jobs.append((name, width))
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda job: _run_reprove(job[0], job[1], args.timeout, args.tactic), jobs))
    method = f"width-correct constraints{', bit-blast' if args.tactic else ''}, {args.timeout:g}s"
    for (name, width), status in zip(jobs, results):
        entry = entries[name]
        entry["notes"] = [n for n in entry["notes"] if not n.startswith(f"{width}-bit:")]
        if status == "proven":
            entry["widths"] = sorted(set(entry["widths"]) | {width})
            entry["notes"].append(f"{width}-bit: proven ({method})")
        else:
            entry["notes"].append(f"{width}-bit: {status.splitlines()[0][:200]} ({method})")
        print(f"{name} {width}: {status.splitlines()[0][:100]}")
    OUT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    proven = sum(1 for e in entries.values() if e["widths"])
    print(f"{proven}/{len(entries)} rules proven at one or more widths; wrote {OUT}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--reprove", action="store_true")
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--tactic", choices=["bitblast"], default=None)
    args = parser.parse_args()
    if args.reprove:
        reprove(args)
        return

    from binja_d810.rules.d810_ops import unsupported_ops
    from binja_d810.third_party.d810.mba.dsl import SymbolicExpression

    entries: dict[str, dict] = {}
    jobs: list[tuple[str, int]] = []
    for name, rule in sorted(_rules().items()):
        notes = []
        symbolic = isinstance(rule.pattern, SymbolicExpression) and isinstance(rule.replacement, SymbolicExpression)
        bad = unsupported_ops(rule.pattern) | unsupported_ops(rule.replacement)
        if getattr(rule, "KNOWN_INCORRECT", False):
            notes.append("excluded: marked KNOWN_INCORRECT upstream")
        elif not symbolic:
            # verify_rule returns True without a proof when either side is not symbolic.
            notes.append("excluded: pattern or replacement is not a symbolic expression")
        elif bad:
            notes.append(f"deferred: unsupported ops {sorted(bad)}")
        entries[name] = {"widths": [], "notes": notes}
        if not notes:
            jobs.extend((name, w) for w in WIDTHS)

    with ThreadPoolExecutor(args.jobs) as pool:
        statuses = list(pool.map(lambda job: _run_job(job[0], job[1], args.timeout), jobs))
    for (name, width), status in sorted(zip(jobs, statuses)):
        if status == "proven":
            entries[name]["widths"].append(width)
        else:
            entries[name]["notes"].append(f"{width}-bit: {status}")

    OUT.write_text(json.dumps({"source": SOURCE, "widths": list(WIDTHS), "rules": entries}, indent=2, sort_keys=True) + "\n")
    proven = sum(1 for e in entries.values() if e["widths"])
    print(f"{proven}/{len(entries)} rules proven at one or more widths; wrote {OUT}")


if __name__ == "__main__":
    main()
