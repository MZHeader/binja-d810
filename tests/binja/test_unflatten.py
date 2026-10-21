"""v4.2: unflattening, in its activity after the MLIL pass, on the flatten fixtures (tests/samples/src/flatten.c)
on 8 builds."""

import pytest
from arch import IDS, MATRIX, sample
from binaryninja import Settings
from flowcases import has, text
from helpers import function_named, marks, open_sample, settle
from unflattencases import NEGATIVES, NOT_FLATTENED, ORDERS, SHAPES

from binja_d810.binja import log, settings, unflatten, workflow
from binja_d810.binja.unflatten import rewrite

ONE = ("x86_64-elf", "O2")  # the build the single-build tests use


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    """(build-level, D-810 on and settled, D-810 off)."""
    on = open_sample(sample("flatten", *request.param), d810=True)
    settle(on)
    off = open_sample(sample("flatten", *request.param), d810=False)
    yield "-".join(request.param), on, off
    on.file.close()
    off.file.close()


def _states(hlil: str, name: str) -> list[str]:
    return [hex(c) for c in SHAPES[name] if has(hlil, hex(c))]


def _first(found: list[int], k: int):
    return found.index(k) if k in found else None


def _lines(func) -> list[str]:
    report = log.latest_reports(func).get("mlil")
    return [] if report is None else report.unflatten


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_a_flattened_function_is_unflattened(builds, name):
    key, on, _ = builds
    if (key, name) in NOT_FLATTENED:
        return
    func = function_named(on, name)
    hlil = text(func)
    assert not _states(hlil, name), hlil
    found = marks(func)
    for earlier, later in ORDERS[name]:
        a, b = _first(found, earlier), _first(found, later)
        assert a is not None and b is not None and a < b, f"mark({earlier}) before mark({later}):\n{hlil}"
    assert _lines(func), workflow.debug_report_text(func)


@pytest.mark.parametrize("name", NEGATIVES)
def test_a_negative_is_unchanged(builds, name):
    key, on, off = builds
    func = function_named(on, name)
    assert not _lines(func), f"{key} {name}: {_lines(func)}"
    assert text(func) == text(function_named(off, name))


def test_the_debug_report_shows_the_rounds(builds):
    _, on, _ = builds
    if not _lines(function_named(on, "fl_tree")):
        pytest.fail("fl_tree was not unflattened")
    assert "unflatten: round 1" in workflow.debug_report_text(function_named(on, "fl_tree"))


def test_a_large_handler_keeps_its_loop_and_result(builds):
    _, on, _ = builds
    func = function_named(on, "fl_large_loop")
    hlil = text(func)
    assert not _states(hlil, "fl_large_loop"), hlil
    assert "while" in hlil or "for" in hlil, hlil
    assert ("0x60" in hlil or "0x5f" in hlil) and "^" in hlil and "+ 7" in hlil, hlil
    assert len(list(func.hlil.instructions)) < 40, hlil  # keep the loop; do not clone its 96 iterations
    assert marks(func) == [91, 92], hlil


def _open_one():
    bv = open_sample(sample("flatten", *ONE), d810=True)
    settle(bv)
    return bv


def _reanalyze(bv, name):
    function_named(bv, name).reanalyze()
    settle(bv)
    return text(function_named(bv, name))


def test_the_setting_turns_unflattening_off_and_on():
    bv = _open_one()
    try:
        assert not _states(text(function_named(bv, "fl_tree")), "fl_tree")
        Settings().set_bool(settings.UNFLATTEN, False, bv)
        assert _states(_reanalyze(bv, "fl_tree"), "fl_tree")
        Settings().set_bool(settings.UNFLATTEN, True, bv)
        assert not _states(_reanalyze(bv, "fl_tree"), "fl_tree")
    finally:
        bv.file.close()


def test_a_failure_keeps_the_function_analyzable(monkeypatch):
    errors = []

    def fail(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(rewrite, "rewrite", fail)
    monkeypatch.setattr(log.logger, "log_error", lambda message, *args: errors.append(message))
    bv = _open_one()
    try:
        func = function_named(bv, "fl_tree")
        assert func.hlil is not None and _states(text(func), "fl_tree")
        assert any("binja-d810 unflatten failed at" in e and "injected" in e for e in errors), errors
    finally:
        bv.file.close()


def test_a_round_over_the_growth_cap_is_not_applied(monkeypatch):
    monkeypatch.setattr(unflatten, "GROWTH", 0)
    monkeypatch.setattr(unflatten, "GROWTH_SLACK", 0)
    bv = _open_one()
    try:
        func = function_named(bv, "fl_tree")
        assert _states(text(func), "fl_tree")
        assert any("not applied" in line for line in _lines(func)), _lines(func)
    finally:
        bv.file.close()
