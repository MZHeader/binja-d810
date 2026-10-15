"""The independent final-HLIL oracle must accept correct results and catch wrong ones."""

import oracle
import pytest
from helpers import function_named, manifest, open_sample, set_types, signature

ENTRIES = [e for e in manifest() if e["category"] in ("d810", "linear")][::9]


@pytest.fixture(scope="module", params=["O0", "O2"])
def simplified(request):
    bv = open_sample(f"mba_{request.param}.dylib", d810=True)
    set_types(bv, {e["name"]: signature(e) for e in ENTRIES})
    yield bv
    bv.file.close()


def test_correct_final_hlil_is_validated(simplified):
    for entry in ENTRIES:
        final = oracle.function_result(function_named(simplified, entry["name"]))
        result = oracle.compare(final, oracle.reference(entry["expected"], entry["width"]), entry["width"])
        assert result.kind in ("proven", "exhaustive", "sampled"), (entry["name"], result)


def _nodes(x) -> int:
    return 1 + sum(_nodes(a) for a in (x[3:] if x[0] == "op" else [x[1]] if x[0] == "bits" else []))


def test_every_single_point_corruption_is_detected(simplified):
    # Only on simplified results: in a still-obfuscated MBA, changing the coefficient of an
    # identically-zero term (c * ((x | y) - (x & ~y) - y)) is an equivalent mutant.
    checked = 0
    for entry in ENTRIES:
        final = oracle.function_result(function_named(simplified, entry["name"]))
        expected = oracle.reference(entry["expected"], entry["width"])
        if _nodes(final) > 2 * _nodes(expected) + 2:
            continue
        for mutant in oracle.mutations(final):
            assert oracle.compare(mutant, expected, entry["width"]).kind == "mismatch", (entry["name"], mutant)
            checked += 1
    assert checked >= len(ENTRIES)


def test_narrow_results_are_checked_exhaustively():
    bv = open_sample("safety_O2.dylib", d810=True)
    try:
        set_types(bv, {"safety_u8": "uint8_t safety_u8(uint8_t x, uint8_t y)"})
        final = oracle.function_result(function_named(bv, "safety_u8"))
        assert oracle.compare(final, oracle.reference("(x ^ y) + 2 * (x & y)", 8), 8).kind == "exhaustive"
    finally:
        bv.file.close()


def test_dependence_on_unknown_upper_bits_is_a_mismatch():
    # x read as 8 bits by the reference, but a final result that uses bit 8 of the register.
    final = ("op", "add", 8, ("bits", ("param", "x"), 1, 8), ("bits", ("param", "y"), 0, 8))
    assert oracle.compare(final, oracle.reference("x + y", 8), 8).kind == "mismatch"


def test_control_flow_is_unvalidated_not_passed():
    bv = open_sample("regression_O2.dylib", d810=True)
    try:
        with pytest.raises(oracle.Unvalidated):
            oracle.function_result(function_named(bv, "reg_crc32_step"))
    finally:
        bv.file.close()
