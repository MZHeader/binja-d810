"""v4.3: outlined flattening (tests/samples/src/outlined.c), on 8 builds: the return stub resolves to the
dispatcher."""

import pytest
from arch import IDS, MATRIX, sample
from flowcases import text
from helpers import function_named, open_sample, settle


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
