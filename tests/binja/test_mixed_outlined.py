"""A state machine mixing self calls with outlined handlers, in all eight builds."""

import pytest
from arch import IDS, MATRIX, sample
from flowcases import text
from helpers import function_named, marks, open_sample, settle
from binja_d810.binja import log, unflatten


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    bv = open_sample(sample("mixed_outlined", *request.param), d810=True)
    settle(bv)
    yield "-".join(request.param), bv, None
    bv.file.close()


def _unflatten_lines(func):
    return [line for report in log.latest_reports(func).values() for line in report.unflatten]


def test_self_calls_do_not_prevent_outlined_handlers_from_being_spliced(builds):
    _, on, _ = builds
    func = function_named(on, "mixed_dispatch")
    hlil = text(func)
    assert set(marks(func)) >= {81, 82, 83, 84}, hlil
    assert "mixed_dispatch(" not in hlil and "mixed_handler(" not in hlil, hlil
    assert any("spliced" in line for line in _unflatten_lines(func)), _unflatten_lines(func)


def test_an_oversized_handler_expansion_keeps_proven_self_call_recovery(monkeypatch):
    original = unflatten.rewrite.size_bound

    def large(graph, planned):
        bound = original(graph, planned)
        return bound + 100_000 if graph.mlil.source_function.name == "mixed_dispatch" else bound

    monkeypatch.setattr(unflatten.rewrite, "size_bound", large)
    bv = open_sample("mixed_outlined_x86_64-elf_O2.so", d810=True)
    try:
        settle(bv)
        func = function_named(bv, "mixed_dispatch")
        hlil = text(func)
        assert "mixed_dispatch(" not in hlil and "mixed_handler(" in hlil, hlil
        assert any("read as a loop" in line for line in _unflatten_lines(func))
        assert not any("spliced" in line for line in _unflatten_lines(func))
    finally:
        bv.file.close()
