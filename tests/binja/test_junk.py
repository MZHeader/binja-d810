"""v4.3: junk loops (tests/samples/src/loops.c), on 8 builds: a loop that only steps its own counter by an odd
amount until it meets a value, with the counter unread after it, is removed; one whose counter is read after it,
or whose step is even, stays."""

import pytest
from arch import IDS, MATRIX, sample
from flowcases import has, text
from helpers import function_named, marks, open_sample, settle

from binja_d810.binja import log

STEPS = {"junk_loop": "0x2f1b", "junk_read": "0x2f1b", "junk_even": "0x2f1a"}  # what each fixture loop adds


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    """(build-level, D-810 on and settled, D-810 off)."""
    on = open_sample(sample("loops", *request.param), d810=True)
    settle(on)
    off = open_sample(sample("loops", *request.param), d810=False)
    yield "-".join(request.param), on, off
    on.file.close()
    off.file.close()


def _lines(func) -> list[str]:
    return [line for report in log.latest_reports(func).values() for line in report.unflatten]


def test_a_loop_that_only_steps_its_counter_is_removed(builds):
    _, on, off = builds
    assert has(text(function_named(off, "junk_loop")), STEPS["junk_loop"])  # the compiler kept the loop
    func = function_named(on, "junk_loop")
    hlil = text(func)
    assert not has(hlil, STEPS["junk_loop"]) and marks(func) == [1, 2], hlil
    assert any("junk loop" in line for line in _lines(func)), _lines(func)


@pytest.mark.parametrize("name", ["junk_read", "junk_even"])
def test_a_loop_whose_counter_is_read_or_may_not_end_stays(builds, name):
    _, on, _ = builds
    func = function_named(on, name)
    hlil = text(func)
    assert has(hlil, STEPS[name]) and not any("junk loop" in line for line in _lines(func)), hlil
