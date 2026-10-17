"""Opaque predicates and branch conditions (v3): folded only when proven; ordinary ones stay."""

import pytest
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, set_types

LOOPS = ("loop_bitwise", "loop_masked", "loop_product")
TYPES = {name: f"uint32_t {name}(uint32_t x, uint32_t y)" for name in
         ("opaque_parity", "opaque_setnz", "opaque_mba", "plain_condition", "mirrored") + LOOPS}
TYPES["load_condition"] = "uint32_t load_condition(uint32_t* p, uint32_t y)"


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def fixtures(request):
    bv = open_sample(sample("predicates", *request.param), d810=True)
    set_types(bv, TYPES)
    yield bv
    bv.file.close()


@pytest.mark.parametrize("name", ["opaque_parity", "opaque_setnz", "opaque_mba", "mirrored"])
def test_opaque_conditions_are_folded_and_the_dead_branch_is_gone(fixtures, name):
    func = function_named(fixtures, name)
    text = "\n".join(map(str, func.hlil.instructions))
    assert "if (" not in text and "0x5a" not in text, text  # the dead branch returns x ^ 0x5a


def test_ordinary_conditions_stay(fixtures):
    func = function_named(fixtures, "plain_condition")
    assert "if (" in "\n".join(map(str, func.hlil.instructions))


def _conditions(func) -> list:
    """The conditions of every if, while, do-while and for in the final HLIL."""
    stack, found = [func.hlil.root], []
    while stack:
        node = stack.pop()
        if hasattr(node, "condition") and node.operation.name in _CONDITIONAL:
            found.append(node.condition)
        stack.extend(o for o in node.operands if hasattr(o, "operation"))
        stack.extend(i for o in node.operands if isinstance(o, list) for i in o if hasattr(i, "operation"))
    return found


_CONDITIONAL = {"HLIL_IF", "HLIL_WHILE", "HLIL_DO_WHILE", "HLIL_FOR"}


@pytest.mark.parametrize("name", LOOPS)
def test_loop_conditions_over_bitwise_operations_stay(fixtures, name):
    # HLIL's loop conditions have size 0: their operands' width must come from the operands.
    from binja_d810.binja import log

    func = function_named(fixtures, name)
    found = _conditions(func)
    assert found and all(c.operation.name != "HLIL_CONST" for c in found), [str(c) for c in found]
    for level, report in log.latest_reports(func).items():
        assert not report.rolled_back and not report.errors, (level, report.errors)


def _loads(func) -> int:
    return sum(1 for i in func.mlil.instructions for n in _walk(i) if n.operation.name == "MLIL_LOAD")


def _walk(e):
    stack = [e]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(o for o in node.operands if hasattr(o, "operation"))


@pytest.mark.parametrize("build,level", MATRIX, ids=IDS)
def test_loads_in_a_predicate_stay(build, level):
    counts = {}
    for d810 in (False, True):
        bv = open_sample(sample("predicates", build, level), d810=d810)
        try:
            set_types(bv, {"load_condition": TYPES["load_condition"]})
            counts[d810] = _loads(function_named(bv, "load_condition"))
        finally:
            bv.file.close()
    assert counts[True] == counts[False] > 0, counts


def test_folds_are_stable_and_disabling_the_plugin_restores_the_branch(fixtures):
    from binaryninja import Settings

    from binja_d810.binja import workflow

    func = function_named(fixtures, "opaque_parity")
    first = [str(i) for i in func.hlil.instructions]
    func.reanalyze()
    fixtures.update_analysis_and_wait()
    func = function_named(fixtures, "opaque_parity")
    assert [str(i) for i in func.hlil.instructions] == first
    Settings().set_bool(workflow.ACTIVITY_SETTING, False, func)
    func.reanalyze()
    fixtures.update_analysis_and_wait()
    func = function_named(fixtures, "opaque_parity")
    try:
        assert "if (" in "\n".join(map(str, func.hlil.instructions))
    finally:
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        fixtures.update_analysis_and_wait()


@pytest.mark.parametrize("build,level", MATRIX, ids=IDS)
def test_the_hlil_pass_alone_folds_proven_conditions_and_leaves_others(build, level):
    # What the MLIL pass cannot express is left to the HLIL pass; a folded HLIL condition stays an
    # `if` on a constant until v3.1 removes dead branches in HLIL (spec v3 section 3).
    from binaryninja import HighLevelILOperation as H

    from binja_d810.binja import workflow

    def conditions(func):
        stack, found = [func.hlil.root], []
        while stack:
            node = stack.pop()
            if node.operation == H.HLIL_IF:
                found.append(node.condition)
            stack.extend(o for o in node.operands if hasattr(o, "operation"))
            stack.extend(i for o in node.operands if isinstance(o, list) for i in o if hasattr(i, "operation"))
        return found

    workflow.set_levels_override({"hlil"})
    bv = open_sample(sample("predicates", build, level), d810=True)
    try:
        set_types(bv, TYPES)
        folded = conditions(function_named(bv, "opaque_parity"))
        assert folded and all(c.operation == H.HLIL_CONST for c in folded), [str(c) for c in folded]
        plain = conditions(function_named(bv, "plain_condition"))
        assert plain and all(c.operation != H.HLIL_CONST for c in plain), [str(c) for c in plain]
    finally:
        workflow.set_levels_override(None)
        bv.file.close()
