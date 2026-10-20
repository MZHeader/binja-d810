"""v3.2: division in opaque predicates (y * y + 3 shapes) and divisions that may trap (tests/samples/src/div.c),
on 8 builds."""

import oracle
import pytest
from arch import IDS, MATRIX, sample
from binaryninja import HighLevelILOperation as H, MediumLevelILOperation as M
from helpers import function_named, open_sample, set_types

from binja_d810.binja import workflow

TYPES = {
    "div_opaque": "uint32_t div_opaque(uint32_t a)",
    "rem_opaque": "uint32_t rem_opaque(uint32_t a)",
    "div_trap": "uint32_t div_trap(uint32_t a, uint32_t b)",
    "div_merge": "uint32_t div_merge(uint32_t a, uint32_t c)",
    "div_keep": "uint32_t div_keep(uint32_t a)",
    "div_loop": "uint32_t div_loop(uint32_t a, uint32_t n)",
}
UNSIGNED = {M.MLIL_DIVU, M.MLIL_DIVU_DP}
SIGNED = {M.MLIL_DIVS, M.MLIL_DIVS_DP}
ARITH = {H.HLIL_ADD, H.HLIL_SUB, H.HLIL_MUL, H.HLIL_NEG}
BITWISE = {H.HLIL_AND, H.HLIL_OR, H.HLIL_XOR, H.HLIL_NOT}


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    on = open_sample(sample("div", *request.param), d810=True)
    off = open_sample(sample("div", *request.param), d810=False)
    set_types(on, TYPES)
    set_types(off, TYPES)
    yield "-".join(request.param), on, off
    on.file.close()
    off.file.close()


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def hlil_only(request):
    """The HLIL pass alone (v3.2 Task 4): what it does when the MLIL pass did not run."""
    workflow.set_levels_override({"hlil"})
    try:
        on = open_sample(sample("div", *request.param), d810=True)
        set_types(on, TYPES)
    finally:
        workflow.set_levels_override(None)
    off = open_sample(sample("div", *request.param), d810=False)
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


def _mlil_operations(func) -> set:
    return {node.operation for instr in func.mlil.instructions for node in _nodes(instr)}


@pytest.mark.parametrize("name", ["div_opaque", "rem_opaque", "div_merge"])
def test_division_predicates_are_folded(builds, name):
    _, on, off = builds
    assert "if (" in _text(function_named(off, name))  # the predicate is there without the plugin
    func = function_named(on, name)
    text = _text(func)
    assert "if (" not in text and "0x5a" not in text, text
    result = oracle.compare(oracle.function_result(func), oracle.reference("a + 1", 32), 32)
    assert result.kind in ("proven", "exhaustive"), (result, text)


def test_a_value_carried_round_a_loop_is_its_one_source(builds):
    # v3.3: the predicate's y * y + 3 reaches the comparison through a merge whose
    # only value from outside is y * y + 3 (the loop copies it round), beside x merged from two values.
    _, on, off = builds
    assert "0x5a" in _text(function_named(off, "div_loop"))
    text = _text(function_named(on, "div_loop"))
    assert "0x5a" not in text and "2 u/" not in text and ".dp." not in text, text


def test_divisions_that_may_trap_stay(builds):
    _, on, off = builds
    before = _mlil_operations(function_named(off, "div_trap"))
    assert before & UNSIGNED and before & SIGNED, before
    func = function_named(on, "div_trap")
    after = _mlil_operations(func)
    assert after & UNSIGNED and after & SIGNED, _text(func)
    assert "0x5a" not in _text(func), _text(func)  # the conditions folded: the divisions' only uses


def test_a_division_that_cannot_trap_is_kept_and_proven(builds):
    _, on, off = builds
    func = function_named(on, "div_keep")
    text = _text(func)
    for statement in func.hlil.instructions:
        kinds = {n.operation for n in _nodes(statement)}
        assert not (kinds & ARITH and kinds & BITWISE), text
    result = oracle.compare(oracle.function_result(func), oracle.function_result(function_named(off, "div_keep")), 32)
    assert result.kind in ("proven", "exhaustive"), (result, text)


def test_the_hlil_pass_folds_a_division_predicate(hlil_only):
    # An HLIL rewrite never changes control flow: the condition becomes a constant (if (true)), and the
    # branch it takes returns a + 1.
    _, on, _ = hlil_only
    func = function_named(on, "div_opaque")
    text = _text(func)
    ifs = [n for statement in func.hlil.instructions for n in _nodes(statement) if n.operation == H.HLIL_IF]
    assert len(ifs) == 1 and ifs[0].condition.operation == H.HLIL_CONST, text
    taken = str(ifs[0].true) if ifs[0].condition.constant else text.split(str(ifs[0].true), 1)[1]
    assert "a + 1" in taken and "0x5a" not in taken, text


def test_the_hlil_pass_keeps_a_division_proven(hlil_only):
    _, on, off = hlil_only
    func = function_named(on, "div_keep")
    text = _text(func)
    for statement in func.hlil.instructions:
        kinds = {n.operation for n in _nodes(statement)}
        assert not (kinds & ARITH and kinds & BITWISE), text
    result = oracle.compare(oracle.function_result(func), oracle.function_result(function_named(off, "div_keep")), 32)
    assert result.kind in ("proven", "exhaustive"), (result, text)
