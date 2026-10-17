"""The opaque-predicate corpus (spec v3 section 6): every predicate that survives compilation is
folded on every build, and the final HLIL computes the live branch's value, or it is listed here
with the reason."""

import json
import os
from collections import Counter
from pathlib import Path

import oracle
import pytest
from arch import IDS, MATRIX, sample
from helpers import BUILD, function_named, open_sample, set_types

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test"))
COVERAGE: dict = {}

# Samples that keep their branch, per build, with the specific technical reason. A listed sample
# that becomes clean fails the test until it is removed here.
KNOWN_MISSES: dict[str, dict[str, str]] = {}


def _manifest() -> list[dict]:
    return json.loads((BUILD / "opaque_manifest.json").read_text())


def _text(func) -> str:
    return "\n".join(map(str, func.hlil.instructions))


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    entries = _manifest()
    types = {e["name"]: f"uint32_t {e['name']}(uint32_t x, uint32_t y, uint32_t z)" for e in entries}
    on = open_sample(sample("opaque", *request.param), d810=True)
    off = open_sample(sample("opaque", *request.param), d810=False)
    set_types(on, types)
    set_types(off, types)
    yield "-".join(request.param), on, off, entries
    on.file.close()
    off.file.close()


def test_opaque_predicates_are_folded(builds):
    key, on, off, entries = builds
    stats: Counter = Counter()
    misses: dict[str, str] = {}
    for entry in entries:
        name = entry["name"]
        if "if (" not in _text(function_named(off, name)):
            stats["folded by the compiler"] += 1
            continue
        func = function_named(on, name)
        if "if (" in _text(func):
            misses[name] = "branch remains: " + _text(func).replace("\n", "; ")[:200]
            continue
        result = oracle.compare(oracle.function_result(func), oracle.reference(entry["live"], 32), 32)
        if result.kind not in ("proven", "exhaustive"):
            misses[name] = f"oracle {result.kind}: {result.detail}"[:200]
            continue
        stats[f"folded:{entry['family']}"] += 1
    COVERAGE[key] = {"stats": dict(stats), "misses": misses}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "opaque_coverage.json").write_text(json.dumps(COVERAGE, indent=2))
    allowed = set(KNOWN_MISSES.get(key, {}))
    assert set(misses) - allowed == set(), {k: v for k, v in misses.items() if k not in allowed}
    assert allowed - set(misses) == set(), f"listed as misses but now folded: {sorted(allowed - set(misses))}"
