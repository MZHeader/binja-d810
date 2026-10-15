import pytest
from helpers import open_sample

from binja_d810.binja import log


@pytest.mark.parametrize("level", ["O0", "O2"])
def test_ordinary_code_gets_zero_rewrites(level):
    bv = open_sample(f"regression_{level}.dylib", d810=True)
    try:
        rewritten = {}
        for func in bv.functions:
            report = log.latest_report(func)
            if report is not None and report.rewrites:
                rewritten[func.name] = report.rewrites
        assert rewritten == {}
    finally:
        bv.file.close()
