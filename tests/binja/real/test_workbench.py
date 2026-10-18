"""The workbench: the plugin on a short list of real-sample functions, one Binary Ninja process for all
of them (minutes, where the full real-sample run takes hours). For developing against real obfuscation.

List the functions in workbench.local.toml (git-ignored; see workbench.example.toml). Each function
is reanalyzed alone with the plugin on; the run writes <out>/workbench/<sample>/<address>.txt (HLIL
with the plugin off and on, both passes' reports) and <out>/workbench/summary.json and summary.txt.
Static analysis only: samples are opened with binaryninja.load and never executed.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path

import binaryninja
import pytest
from binaryninja import Settings
from measure import hlil_counts
from helpers import settle

from binja_d810.binja import limits, log, settings, workflow

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test")) / "workbench"
LIST = Path(os.environ.get("BINJA_D810_WORKBENCH", Path(__file__).with_name("workbench.local.toml")))


def _entries() -> list[tuple[str, list[int]]]:
    if not LIST.exists():
        return []
    table = tomllib.loads(LIST.read_text())
    return [(sample, [int(a, 16) for a in entry.get("functions", [])]) for sample, entry in table.items()]


def _text(func) -> list[str]:
    return [] if func is None or func.hlil is None else [str(i) for i in func.hlil.instructions]


def _flow_notes(bv, address: int) -> list[str]:
    try:
        from binja_d810.binja.flow import apply
    except ImportError:  # a revision without the flow fixes
        return []
    return apply.notes_for(bv, address)


def _row(sample: str, address: int, before, after, reports: dict, seconds: float) -> dict:
    (before_text, b), (after_text, a) = before, after
    row = {"sample": sample, "function": hex(address), "seconds": round(seconds, 2)}
    if a is None:
        return {**row, "lost": True}  # Binary Ninja gave up on the function: no HLIL
    return {**row, "hlil_lines": [len(before_text), len(after_text)], "mba_roots": [b.mba_roots, a.mba_roots],
            "mba_nodes": [b.mba_nodes, a.mba_nodes], "conditionals": [b.conditionals, a.conditionals],
            "rewrites": {level: len(r.rewrites) for level, r in reports.items()},
            "exhausted": [level for level, r in reports.items() if r.budget_exhausted],
            "rolled_back": [level for level, r in reports.items() if r.rolled_back],
            "skips": sorted({s.reason.split(":")[0] for r in reports.values() for s in r.skipped})}


@pytest.mark.skipif(not LIST.exists(), reason="no workbench.local.toml")
def test_workbench():
    root = Path(os.environ["BINJA_D810_SAMPLES"])
    rows = []
    for sample, addresses in _entries():
        options = {workflow.ACTIVITY_SETTING: False}
        if os.environ.get("BINJA_D810_FUNCTION_SECONDS"):
            options[settings.FUNCTION_SECONDS] = int(os.environ["BINJA_D810_FUNCTION_SECONDS"])
        # As if the binary were opened with D-810 enabled: room for the whole budget in Binary Ninja's own
        # limit, set when the binary opens (binja_d810.binja.limits), though each function is enabled below.
        options.update(limits.opening_options(options.get(settings.FUNCTION_SECONDS,
                                                           Settings().get_integer(settings.FUNCTION_SECONDS))))
        bv = binaryninja.load(str(root / sample), options=options)
        assert bv is not None, sample
        try:
            for address in addresses:
                func = bv.get_function_at(address)
                if func is None:
                    rows.append({"sample": sample, "function": hex(address), "missing": True})
                    continue
                before = (_text(func), hlil_counts(func))
                Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
                started = time.perf_counter()
                func.reanalyze()
                settle(bv, seconds=int(os.environ.get("BINJA_D810_SETTLE_SECONDS", "120")))
                seconds = time.perf_counter() - started
                func = bv.get_function_at(address)
                after = (_text(func), hlil_counts(func) if func is not None and func.hlil is not None else None)
                reports = log.latest_reports(func) if func is not None else {}
                rows.append(_row(sample, address, before, after, reports, seconds))
                out = OUT / sample
                out.mkdir(parents=True, exist_ok=True)
                lines = [f"{sample} {address:#x}: {seconds:.1f} s", "", "=== HLIL, plugin off ===", *before[0], "",
                         "=== HLIL, plugin on ===", *after[0], ""]
                lines += [f"=== {level} report ===\n{log.render(r)}\n" for level, r in reports.items()]
                lines += ["=== flow ===", *_flow_notes(bv, address), ""]
                (out / f"{address:#x}.txt").write_text("\n".join(lines))
        finally:
            bv.file.close()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(rows, indent=1))
    (OUT / "summary.txt").write_text("\n".join(
        f"{r['sample']:18} {r['function']:>12} " + (
            "lost its HLIL" if r.get("lost") else "not a function" if r.get("missing") else
            f"{r['seconds']:6.1f} s  MBA roots {r['mba_roots'][0]}->{r['mba_roots'][1]}  nodes {r['mba_nodes'][0]}->"
            f"{r['mba_nodes'][1]}  ifs {r['conditionals'][0]}->{r['conditionals'][1]}  lines {r['hlil_lines'][0]}->"
            f"{r['hlil_lines'][1]}  rewrites {r['rewrites']}  exhausted {r['exhausted']}  skips {r['skips']}")
        for r in rows))
