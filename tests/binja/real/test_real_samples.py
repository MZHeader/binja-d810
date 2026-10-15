"""Real-sample measurement, one binary per Binary Ninja process (see scripts/real_samples.sh).

Per binary:
1. load with the plugin off and record the "before" figures (HLIL structural counters, plus a
   read-only scan with the plugin's own MLIL lifter and simplifier: would-be rewrites only);
2. enable the plugin on every function with MBA candidates, reanalyze it, and record the "after"
   figures, the plugin's reports and per-function timing;
3. write $BINJA_D810_PYTEST_OUT/real/<sample>.json (git-ignored, never committed).

Static analysis only: binaryninja.load, never execution, debugging or emulation.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

import binaryninja
import pytest
from binaryninja import Settings
from measure import RootCounts, hlil_counts

import binja_d810
from binja_d810.binja import log, workflow

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test")) / "real"
MLIL_SCAN_SECONDS = 600
MAX_PLUGIN_FUNCTIONS = 400
SETTINGS_RECORDED = (
    "analysis.limits.maxFunctionAnalysisTime", "analysis.limits.maxFunctionSize",
    "analysis.limits.maxFunctionUpdateCount", "binja_d810.maxNodes", "binja_d810.maxVariables",
    "binja_d810.maxIterations", "binja_d810.functionSeconds",
)


def _sample() -> Path:
    directory = os.environ.get("BINJA_D810_SAMPLES")
    name = os.environ.get("BINJA_D810_SAMPLE")
    if not directory or not name:
        pytest.skip("set BINJA_D810_SAMPLES and BINJA_D810_SAMPLE")
    return Path(directory) / name


def _versions() -> dict:
    out = {"binaryninja": binaryninja.core_version()}
    try:
        import z3
        out["z3"] = z3.get_version_string()
    except ImportError:
        out["z3"] = None
    try:
        from importlib.metadata import version
        out["d810_cobra"] = version("d810-cobra")
    except Exception:
        out["d810_cobra"] = None
    plugin = Path(binja_d810.__file__).resolve().parent
    out["plugin_path_kind"] = "worktree" if ".local" in plugin.parts else "repo"
    try:
        out["plugin_revision"] = subprocess.run(
            ["git", "-C", str(plugin), "describe", "--always", "--dirty", "--tags"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        out["plugin_revision"] = None
    return out


def _settings(bv) -> dict:
    s = Settings()
    out = {}
    for key in SETTINGS_RECORDED:
        if s.contains(key):
            try:
                out[key] = s.get_integer(key, bv)
            except Exception:
                out[key] = None
    return out


def _mlil_scan(bv, deadline: float) -> tuple[dict, dict]:
    """Read-only scan with the plugin's own lifter and simplifier. Nothing is rewritten."""
    stats: Counter = Counter()
    per_func: dict = {}
    try:
        from binja_d810.binja.lift import candidate_roots, lift
        from binja_d810.ir.expr import is_mba
        from binja_d810.rules.registry import load_rules
        from binja_d810.semantic.backend import default_backends
        from binja_d810.simplify import simplify_expression
    except ImportError as exc:
        return {"unavailable": repr(exc)}, per_func
    rules, backends = load_rules(), default_backends()
    for func in bv.functions:
        if time.monotonic() > deadline:
            stats["truncated"] = 1
            break
        mlil = func.mlil
        if mlil is None:
            stats["functions_without_mlil"] += 1
            continue
        found = Counter()
        for instr in mlil.instructions:
            for root in candidate_roots(instr):
                ssa = root.ssa_form
                unchecked = _lift(lift, ssa, instr, check=False)
                if unchecked is not None and is_mba(unchecked):
                    found["mba_roots_unchecked"] += 1
                checked = _lift(lift, ssa, instr, check=True)
                if checked is None or not is_mba(checked):
                    continue
                found["mba_roots"] += 1
                outcome = simplify_expression(checked, rules, backends)
                if outcome.rewrite is not None:
                    found["would_rewrite"] += 1
                    found["src:" + outcome.rewrite.source.split("+")[0].split(":")[0]] += 1
                    verdict = getattr(outcome.rewrite, "verdict", None)
                    if verdict is not None:
                        found[f"verdict:{verdict.name}"] += 1
                elif outcome.skipped:
                    found["skipped"] += 1
                found["refuted"] += getattr(outcome, "refuted", 0)
        if found:
            per_func[func.start] = dict(found)
            stats.update(found)
            stats["functions_with_mba_roots_unchecked"] += found["mba_roots_unchecked"] > 0
            stats["functions_with_mba_roots"] += found["mba_roots"] > 0
    return dict(stats), per_func


def _lift(lift, ssa, instr, *, check: bool):
    result = lift(ssa, root_instr=instr, check_versions=check)
    return getattr(result, "expr", result)  # v2 may return a LiftResult


class _Timer:
    """Wraps the activity's engine call to time plugin work per function (v1 has no timing)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seconds: dict[int, list[float]] = {}
        self.real = workflow.simplify_function

    def __enter__(self):
        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return self.real(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                func = _function_of(args)
                with self.lock:
                    self.seconds.setdefault(func, []).append(elapsed)
        workflow.simplify_function = timed
        return self

    def __exit__(self, *exc):
        workflow.simplify_function = self.real


def _function_of(args) -> int:
    for arg in args:
        source = getattr(arg, "source_function", None)
        if source is not None:
            return source.start
    return -1


def _summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {"n": len(values), "median": round(statistics.median(values), 4), "p95": round(p95, 4),
            "max": round(ordered[-1], 4), "total": round(sum(values), 2)}


def _report_summary(report, into: Counter, skips: Counter) -> None:
    if report is None:
        into["no_report"] += 1
        return
    into["rewrites"] += len(report.rewrites)
    for record in report.rewrites:
        source = record[1] if isinstance(record, tuple) else record.source
        into["src:" + source.split("+")[0].split(":")[0]] += 1
        verdict = getattr(record, "verdict", None)
        if verdict is not None:
            into[f"verdict:{getattr(verdict, 'name', verdict)}"] += 1
            into[f"method:{record.method}"] += 1
    into["errors"] += len(report.errors)
    into["rolled_back"] += bool(report.rolled_back)
    into["capped"] += bool(report.capped)
    into["refuted"] += getattr(report, "refuted", 0)
    for name in ("budget_exhausted", "overrun"):
        into[name] += bool(getattr(report, name, False))
    for entry in report.skipped:
        reason = entry[1] if isinstance(entry, tuple) else entry.reason
        skips[reason.split(":")[0]] += 1


def test_sample():
    path = _sample()
    data = path.read_bytes()
    result: dict = {"sample": path.name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                    "versions": _versions(), "status": "started"}
    del data
    OUT.mkdir(parents=True, exist_ok=True)
    out_file = OUT / f"{path.name}.json"
    out_file.write_text(json.dumps(result, indent=1))

    start = time.perf_counter()
    bv = binaryninja.load(str(path), options={workflow.ACTIVITY_SETTING: False})
    assert bv is not None, f"Binary Ninja could not load {path.name}"
    try:
        result.update(arch=str(bv.arch), platform=str(bv.platform), view_type=bv.view_type,
                      load_s=round(time.perf_counter() - start, 1), settings=_settings(bv))
        funcs = list(bv.functions)
        result["functions_total"] = len(funcs)
        result["functions_skipped_by_core"] = sum(1 for f in funcs if f.analysis_skipped)

        before: dict[int, RootCounts] = {f.start: hlil_counts(f) for f in funcs}
        total_before = RootCounts()
        for counts in before.values():
            total_before.add(counts)
        result["before"] = total_before.to_json()
        result["functions_with_hlil_mba"] = sum(1 for c in before.values() if c.mba_roots)

        scan_start = time.perf_counter()
        scan, per_func = _mlil_scan(bv, time.monotonic() + MLIL_SCAN_SECONDS)
        result["mlil_scan"] = scan
        result["mlil_scan_s"] = round(time.perf_counter() - scan_start, 1)

        candidates = sorted({f.start for f in funcs if before[f.start].mba_roots} |
                            {start for start, found in per_func.items() if found.get("mba_roots_unchecked")})
        result["candidate_functions"] = len(candidates)
        result["plugin_functions_truncated"] = len(candidates) > MAX_PLUGIN_FUNCTIONS
        candidates = candidates[:MAX_PLUGIN_FUNCTIONS]
        out_file.write_text(json.dumps(result, indent=1))

        totals, skips = Counter(), Counter()
        before_c, after_c = RootCounts(), RootCounts()
        reanalysis, per_function = [], []
        with _Timer() as timer:
            for address in candidates:
                func = bv.get_function_at(address)
                if func is None:
                    totals["function_vanished"] += 1
                    continue
                if address not in before:  # discovered after the first snapshot
                    before[address] = hlil_counts(func)
                    totals["functions_discovered_late"] += 1
                Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
                t0 = time.perf_counter()
                func.reanalyze()
                bv.update_analysis_and_wait()
                reanalysis.append(time.perf_counter() - t0)
                func = bv.get_function_at(address)
                if func is None or func.hlil is None:
                    totals["analysis_failures"] += 1
                    reason = getattr(func, "analysis_skip_reason", None) if func is not None else "function vanished"
                    totals[f"analysis_failure:{getattr(reason, 'name', reason)}"] += 1
                    per_function.append({"start": hex(address), "mba_before": before[address].mba_roots,
                                         "analysis_failure": str(reason),
                                         "plugin_s": round(sum(timer.seconds.get(address, [])), 4)})
                    continue
                after = hlil_counts(func)
                before_c.add(before[address])
                after_c.add(after)
                report = log.latest_report(func)
                _report_summary(report, totals, skips)
                per_function.append({"start": hex(address), "mba_before": before[address].mba_roots,
                                     "mba_after": after.mba_roots,
                                     "rewrites": len(report.rewrites) if report else 0,
                                     "plugin_s": round(sum(timer.seconds.get(address, [])), 4)})
        plugin_s = [sum(v) for k, v in timer.seconds.items() if k in set(candidates)]
        result["candidates_before"] = before_c.to_json()
        result["candidates_after"] = after_c.to_json()
        result["plugin"] = dict(totals)
        result["skip_reasons"] = dict(skips.most_common())
        result["timing"] = {"plugin_per_function": _summary(plugin_s),
                            "reanalysis_per_function": _summary(reanalysis)}
        per_function.sort(key=lambda r: -r["mba_before"])
        result["top_functions"] = per_function[:25]
        result["status"] = "complete"
        result["total_s"] = round(time.perf_counter() - start, 1)
    finally:
        out_file.write_text(json.dumps(result, indent=1))
        bv.file.close()
