"""Prove each vendored d810 rule with Z3 at 8/16/32/64 bits; write binja_d810/rules/verified_rules.json.

Usage: uv run python scripts/verify_rules.py [--timeout 60] [--jobs N]
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

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
