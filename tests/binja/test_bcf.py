"""v3.1: OLLVM-style bogus control flow and the shapes around it (tests/samples/src/bcf.c), on 8 builds."""

import oracle
import pytest
from arch import IDS, MATRIX, sample
from binaryninja import HighLevelILOperation as H
from helpers import function_named, open_sample, set_types

from binja_d810.binja import workflow

TYPES = {
    "bcf_chain": "uint32_t bcf_chain(uint32_t a)",
    "bcf_merge": "uint32_t bcf_merge(uint32_t a, uint32_t c)",
    "const_use": "uint32_t const_use(uint32_t a)",
    "align_gate": "uint32_t align_gate(uint32_t p, uint32_t n)",
    "const_after_merge": "uint32_t const_after_merge(uint32_t a, uint32_t c)",
    "align_gate2": "uint32_t align_gate2(uint32_t p, uint32_t n)",
    "align_call": "uint32_t align_call(uint32_t p, uint32_t n)",
}
# Builds where a fixture is not folded, with the reason; a listed miss that folds fails the test.
KNOWN_MISSES = {
    ("arm64-O2", "bcf_merge"): "each arm's k is computed from x after x's register is reused; the unchecked "
                               "(constant-only) lift is not used for values the checked lift saw partly "
                               "(folding two such values in mba_linear_030 made Binary Ninja's HLIL drop a definition)",
    ("arm64-O2", "const_after_merge"): "k is computed from x after x's register is reused (as bcf_merge)",
}
ARITH = {H.HLIL_ADD, H.HLIL_SUB, H.HLIL_MUL, H.HLIL_NEG}
BITWISE = {H.HLIL_AND, H.HLIL_OR, H.HLIL_XOR, H.HLIL_NOT}


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    on = open_sample(sample("bcf", *request.param), d810=True)
    off = open_sample(sample("bcf", *request.param), d810=False)
    set_types(on, TYPES)
    set_types(off, TYPES)
    yield "-".join(request.param), on, off
    on.file.close()
    off.file.close()


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def hlil_only(request):
    """The HLIL pass alone (v3.2 Task 6): its own statement gate, without the MLIL pass's."""
    workflow.set_levels_override({"hlil"})
    try:
        on = open_sample(sample("bcf", *request.param), d810=True)
        set_types(on, TYPES)
    finally:
        workflow.set_levels_override(None)
    off = open_sample(sample("bcf", *request.param), d810=False)
    set_types(off, TYPES)
    yield "-".join(request.param), on, off
    on.file.close()
    off.file.close()


def _text(func) -> str:
    return "\n".join(map(str, func.hlil.instructions))


def _nodes(e):
    stack = [e]
    while stack:
        node = stack.pop()
        yield node
        for operand in node.operands:
            if hasattr(operand, "operation"):
                stack.append(operand)
            elif isinstance(operand, list):
                stack.extend(o for o in operand if hasattr(o, "operation"))


@pytest.mark.parametrize("name", ["bcf_chain", "bcf_merge"])
def test_bogus_control_flow_is_folded(builds, name):
    key, on, _ = builds
    func = function_named(on, name)
    text = _text(func)
    if (key, name) in KNOWN_MISSES:
        assert "if (" in text, f"listed as a miss but folded: {(key, name)}"
        return
    assert "if (" not in text and "0x5a" not in text, text
    result = oracle.compare(oracle.function_result(func), oracle.reference("a + 1", 32), 32)
    assert result.kind in ("proven", "exhaustive"), (result, text)


def test_a_folded_constant_reaches_its_uses(builds):
    _, on, _ = builds
    func = function_named(on, "const_use")
    constants = [str(i) for i in func.hlil.instructions
                 if i.operation in (H.HLIL_VAR_INIT, H.HLIL_ASSIGN) and i.src.operation == H.HLIL_CONST]
    assert not constants, _text(func)


def _mixed(func) -> list[str]:
    return [str(i) for i in func.hlil.instructions
            if {n.operation for n in _nodes(i)} & ARITH and {n.operation for n in _nodes(i)} & BITWISE]


@pytest.mark.parametrize("name", ["align_gate", "align_gate2", "align_call"])
def test_a_rewrite_never_makes_its_statement_mixed(builds, name):
    _, on, off = builds
    before, after = _mixed(function_named(off, name)), _mixed(function_named(on, name))
    assert len(after) <= len(before), (before, after)


@pytest.mark.parametrize("name", ["align_gate", "align_gate2", "align_call"])
def test_the_hlil_pass_never_makes_its_statement_mixed(hlil_only, name):
    _, on, off = hlil_only
    before, after = _mixed(function_named(off, name)), _mixed(function_named(on, name))
    assert len(after) <= len(before), (before, after)


def test_a_constant_read_after_a_merge_reaches_that_read_only(builds):
    from binja_d810.binja import log

    key, on, off = builds
    func = function_named(on, "const_after_merge")
    reports = log.latest_reports(func)
    assert all(not r.rolled_back and not any(e.source == "engine" for e in r.errors) for r in reports.values()), \
        {level: r.errors for level, r in reports.items()}
    text = _text(func)
    if (key, "const_after_merge") in KNOWN_MISSES:
        assert "sink(1)" not in text, f"listed as a miss but folded: {key}"
        return
    assert "sink(1)" in text, text  # k's read; m's reads keep m
    assert text.count("sink(") == 2 and text.count("sink(1)") == 1, text
