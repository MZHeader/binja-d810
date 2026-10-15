import pytest
from arch import IDS, MATRIX, sample
from helpers import open_sample

from binja_d810.binja import log


@pytest.mark.parametrize("build,level", MATRIX, ids=IDS)
def test_ordinary_code_gets_zero_rewrites(build, level):
    bv = open_sample(sample("regression", build, level), d810=True)
    try:
        rewritten = {}
        for func in bv.functions:
            report = log.latest_report(func)
            if report is not None and report.rewrites:
                rewritten[func.name] = report.rewrites
        assert rewritten == {}
    finally:
        bv.file.close()
