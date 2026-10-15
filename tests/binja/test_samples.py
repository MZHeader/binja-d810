import json
import os
import re
import time
from pathlib import Path

import oracle
import pytest
from arch import IDS, MATRIX, sample
from helpers import function_named, lift_return, manifest, open_sample, set_types, signature, strip_versions

from binja_d810.binja import log, settings
from binja_d810.ir.expr import UnOp, is_mba, node_count, to_str, variables
from binja_d810.ir.parse import parse
from binja_d810.semantic import cobra_shim
from binja_d810.semantic.verify import Verdict, check_equivalent

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test"))
COVERAGE: dict = {"seconds": {}}
BITWISE = re.compile(r"\^(?!=)|(?<![&])&(?![&=])|(?<![|])\|(?![|=])|~|\bnot\.")

# Samples that stay obfuscated, per build, with the specific technical reason. Any other failure is
# a regression; a listed sample that starts passing fails the test until it is removed here.
_SPLIT64 = "64-bit values live in 32-bit register pairs joined by adc/sbb carries (i686), not modelled"
_I686_64 = {f"mba_linear_{i:03d}": _SPLIT64 for i in range(4, 50, 5)}  # every fifth linear sample is 64-bit
KNOWN_MISSES: dict[str, dict[str, str]] = {
    "i686-pe-O0": dict(_I686_64),
    "i686-pe-O2": dict(_I686_64),
}


# Samples whose final HLIL the oracle cannot read, with the reason. Empty: any unvalidated sample
# fails the test, so a result can never pass merely because nothing refuted it.
ORACLE_UNVALIDATED: dict[str, dict[str, str]] = {"i686-pe-O0": dict(_I686_64), "i686-pe-O2": dict(_I686_64)}


def _oracle(func, entry) -> tuple[str, str]:
    try:
        final = oracle.function_result(func)
    except oracle.Unvalidated as exc:
        return "unvalidated", str(exc)
    result = oracle.compare(final, oracle.reference(entry["expected"], entry["width"]), entry["width"])
    return result.kind, result.detail


def _hlil_clean(func, expected) -> bool:
    """The function's HLIL has no more bitwise operators than the expected answer."""
    text = "\n".join(str(i) for i in func.hlil.instructions)
    return len(BITWISE.findall(text)) <= len(BITWISE.findall(to_str(expected)))


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def simplified(request):
    build, level = request.param
    entries = manifest()
    start = time.perf_counter()
    # Coverage, not latency: with all 96 functions analyzed in parallel, Python's GIL stretches each
    # function's planning well past the default 5 s (tests/binja/test_budget.py covers the budget).
    bv = open_sample(sample("mba", build, level), d810=True,
                     options={settings.FUNCTION_SECONDS: 60, "analysis.limits.maxFunctionAnalysisTime": 180_000})
    set_types(bv, {e["name"]: signature(e) for e in entries})
    key = f"{build}-{level}"
    COVERAGE["seconds"][key] = round(time.perf_counter() - start, 1)
    yield key, bv, entries
    bv.file.close()


def test_samples(simplified):
    level, bv, entries = simplified
    stats: dict = {}
    refuted = []
    mismatched, unvalidated, analysis_failures = [], [], []
    for entry in entries:
        func = function_named(bv, entry["name"])
        expected = parse(entry["expected"], entry["width"])
        if func.hlil is None:
            analysis_failures.append((entry["name"], str(func.analysis_skip_reason)))
            continue
        lifted = lift_return(func)
        plain = strip_versions(lifted) if lifted is not None else None
        if plain is not None and plain.width > entry["width"]:
            plain = UnOp("trunc", plain, entry["width"])  # x64 returns zx.q(32-bit result)
        elif plain is not None and plain.width < entry["width"]:
            plain = None  # i686 returns a 64-bit value in edx:eax; the oracle checks the final HLIL
        cat = stats.setdefault(entry["category"], {"total": 0, "simplified": 0, "hlil_clean": 0, "failures": [],
                                                   "oracle": {}})
        cat["total"] += 1
        kind, detail = _oracle(func, entry)
        cat["oracle"][kind] = cat["oracle"].get(kind, 0) + 1
        if kind == "mismatch":
            mismatched.append((entry["name"], detail))
        elif kind == "unvalidated" and entry["name"] not in ORACLE_UNVALIDATED.get(level, {}):
            unvalidated.append((entry["name"], detail))
        # The MLIL lift is comparable only when it is over the parameters alone; otherwise (an opaque
        # edx:eax pair on i686) the oracle on the final HLIL is the check.
        comparable = plain is not None and {v.name for v in variables(plain)} <= {"x", "y", "z"}
        if comparable and check_equivalent(plain, expected, use_z3=False) is Verdict.REFUTED:
            refuted.append(entry["name"])
        # Some expected answers mix arithmetic and bitwise ops themselves (x - (y & z)).
        demixed = plain is not None and (not is_mba(plain) or is_mba(expected))
        if demixed and node_count(plain) <= node_count(expected) + 2:
            cat["simplified"] += 1
        reports = log.latest_reports(func)
        if any(r.budget_exhausted for r in reports.values()):
            cat.setdefault("budget_exhausted", []).append(entry["name"])
        cat.setdefault("plugin_seconds", []).append(round(sum(r.total_seconds for r in reports.values()), 3))
        if _hlil_clean(func, expected):
            cat["hlil_clean"] += 1
        else:
            cat["failures"].append(entry["name"])
    COVERAGE[level] = stats
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "coverage.json").write_text(json.dumps(COVERAGE, indent=2))

    assert analysis_failures == [], f"no HLIL after analysis: {analysis_failures}"
    assert refuted == [], f"semantics changed: {refuted}"
    assert mismatched == [], f"final HLIL differs from the expected computation: {mismatched}"
    assert unvalidated == [], f"final HLIL not validated: {unvalidated}"
    allowed = set(KNOWN_MISSES.get(level, {}))
    if cobra_shim.load() is None:
        allowed |= {e["name"] for e in entries if e["category"] == "poly"}
    failed = {name for s in stats.values() for name in s["failures"]}
    assert failed - allowed == set(), sorted(failed - allowed)
    listed = set(KNOWN_MISSES.get(level, {}))
    assert listed - failed == set(), f"listed as misses but now clean, remove them: {sorted(listed - failed)}"
    assert COVERAGE["seconds"][level] < 600
