"""v4.3: outlined flattening (tests/samples/src/outlined.c), on 8 builds: the return stub resolves to the
dispatcher; a dispatcher that calls itself in tail position is unflattened; a machine passing its own frame's
address, and ordinary recursion, stay."""

import pytest
from arch import IDS, MATRIX, expect_miss, sample
from flowcases import text
from helpers import function_named, open_sample, settle

from binja_d810.binja import log


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    """(build-level, D-810 on and settled, D-810 off)."""
    on = open_sample(sample("outlined", *request.param), d810=True)
    settle(on)
    off = open_sample(sample("outlined", *request.param), d810=False)
    yield "-".join(request.param), on, off
    on.file.close()
    off.file.close()


def test_the_return_stub_resolves(builds):
    _, on, _ = builds
    hlil = text(function_named(on, "ol_stub"))
    assert "ol_dispatch(" in hlil and "ol_back" not in hlil and "data_" not in hlil, hlil


SC_STATES = ("0x2d81f6a3", "0x49e07b12", "0xc5a9e84", "0x71f3d250")


def _unflatten_lines(func) -> list[str]:
    return [line for report in log.latest_reports(func).values() for line in report.unflatten]


def test_a_dispatcher_calling_itself_in_tail_position_is_unflattened(request, builds):
    # The first state is the caller's, so the entry still tests each state once; every self call jumps straight
    # to its next state's handler.
    key, on, _ = builds
    expect_miss(request, "sc_dispatch", key)
    func = function_named(on, "sc_dispatch")
    hlil = text(func)
    assert all(hlil.count(s) <= 1 for s in SC_STATES) and "sc_dispatch(" not in hlil and "sc_self" not in hlil, hlil
    assert any("read as a loop" in line for line in _unflatten_lines(func)), _unflatten_lines(func)


@pytest.mark.parametrize("name, pointer", [("lf_dispatch", "lf_self"), ("rc_count", "rc_self")])
def test_recursion_through_the_frame_or_without_a_machine_stays(builds, name, pointer):
    # The pointer may still resolve (v4.0); the call stays a call (a PE names the pointer data_...). A tail jump the
    # compiler emitted, once resolved to the function's own start, is the binary's own loop.
    _, on, _ = builds
    func = function_named(on, name)
    hlil = text(func)
    assert any(token in hlil for token in (f"{name}(", pointer, "jump(", "data_", "while (")), hlil
    assert not any("read as a loop" in line for line in _unflatten_lines(func)), _unflatten_lines(func)
