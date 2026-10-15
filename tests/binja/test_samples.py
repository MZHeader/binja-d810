import json
import os
import re
import time
from pathlib import Path

import oracle
import pytest
from helpers import function_named, lift_return, manifest, open_sample, set_types, signature, strip_versions

from binja_d810.ir.expr import is_mba, node_count, to_str
from binja_d810.ir.parse import parse
from binja_d810.semantic import cobra_shim
from binja_d810.semantic.verify import Verdict, check_equivalent

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test"))
COVERAGE: dict = {"seconds": {}}
BITWISE = re.compile(r"\^(?!=)|(?<![&])&(?![&=])|(?<![|])\|(?![|=])|~|\bnot\.")

# -O2 samples that stay obfuscated in v1: clang reuses the input registers before the arithmetic
# combines the bitwise terms, so no single instruction can express the result (see the environment
# spike doc). Any other failure is a regression; a listed sample that starts passing should be removed.
KNOWN_MISSES: dict[str, set[str]] = {
    "O0": set(),
    "O2": {
        "mba_linear_001", "mba_linear_002", "mba_linear_006", "mba_linear_013", "mba_linear_014",
        "mba_linear_015", "mba_linear_018", "mba_linear_019", "mba_linear_030", "mba_linear_031",
        "mba_linear_034", "mba_linear_039", "mba_linear_040", "mba_linear_043", "mba_linear_047",
        "mba_linear_048", "mba_linear_049",
    },
}


# Samples whose final HLIL the oracle cannot read, with the reason. Empty: any unvalidated sample
# fails the test, so a result can never pass merely because nothing refuted it.
ORACLE_UNVALIDATED: dict[str, dict[str, str]] = {"O0": {}, "O2": {}}


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


@pytest.fixture(scope="module", params=["O0", "O2"])
def simplified(request):
    entries = manifest()
    start = time.perf_counter()
    bv = open_sample(f"mba_{request.param}.dylib", d810=True)
    set_types(bv, {e["name"]: signature(e) for e in entries})
    COVERAGE["seconds"][request.param] = round(time.perf_counter() - start, 1)
    yield request.param, bv, entries
    bv.file.close()


def test_samples(simplified):
    level, bv, entries = simplified
    stats: dict = {}
    refuted = []
    mismatched, unvalidated = [], []
    for entry in entries:
        func = function_named(bv, entry["name"])
        expected = parse(entry["expected"], entry["width"])
        lifted = lift_return(func)
        plain = strip_versions(lifted) if lifted is not None else None
        cat = stats.setdefault(entry["category"], {"total": 0, "simplified": 0, "hlil_clean": 0, "failures": [],
                                                   "oracle": {}})
        cat["total"] += 1
        kind, detail = _oracle(func, entry)
        cat["oracle"][kind] = cat["oracle"].get(kind, 0) + 1
        if kind == "mismatch":
            mismatched.append((entry["name"], detail))
        elif kind == "unvalidated" and entry["name"] not in ORACLE_UNVALIDATED[level]:
            unvalidated.append((entry["name"], detail))
        if plain is not None and check_equivalent(plain, expected, use_z3=False) is Verdict.REFUTED:
            refuted.append(entry["name"])
        # Some expected answers mix arithmetic and bitwise ops themselves (x - (y & z)).
        demixed = plain is not None and (not is_mba(plain) or is_mba(expected))
        if demixed and node_count(plain) <= node_count(expected) + 2:
            cat["simplified"] += 1
        if _hlil_clean(func, expected):
            cat["hlil_clean"] += 1
        else:
            cat["failures"].append(entry["name"])
    COVERAGE[level] = stats
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "coverage.json").write_text(json.dumps(COVERAGE, indent=2))

    assert refuted == [], f"semantics changed: {refuted}"
    assert mismatched == [], f"final HLIL differs from the expected computation: {mismatched}"
    assert unvalidated == [], f"final HLIL not validated: {unvalidated}"
    allowed = set(KNOWN_MISSES[level])
    if cobra_shim.load() is None:
        allowed |= {e["name"] for e in entries if e["category"] == "poly"}
    unexpected = {name for s in stats.values() for name in s["failures"]} - allowed
    assert unexpected == set(), sorted(unexpected)
    assert COVERAGE["seconds"][level] < 600
