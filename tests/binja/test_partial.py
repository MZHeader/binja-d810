"""Partial-register reads and writes on x86, from exact assembly fixtures.

Each fixture computes (x ^ y) + 2 * (x & y) on part of a register and keeps the rest; the reference
is written out in tests/samples/src/partial_*.S. The oracle models parameters as full registers
with unknown upper bits, so a result that invents upper bits is a mismatch.
"""

import oracle
import pytest
from helpers import function_named, open_sample, set_types

from binja_d810.binja import log

CASES = {
    "partial_x86_64.so": {
        "part_al": ("uint8_t part_al(uint8_t x, uint8_t y)", "x + y", 8),
        "part_ah": ("uint32_t part_ah(uint32_t x, uint32_t y)",
                    "(x & 0xffff00ff) | ((((x >> 8) + (y >> 8)) & 0xff) << 8)", 32),
        "part_ax": ("uint32_t part_ax(uint32_t x, uint32_t y)", "(x & 0xffff0000) | ((x + y) & 0xffff)", 32),
        "part_eax_zx": ("uint64_t part_eax_zx(uint64_t x, uint64_t y)", "(x + y) & 0xffffffff", 64),
        "part_overlap": ("uint32_t part_overlap(uint32_t x, uint32_t y)",
                         "((x & 0xffffff00) | ((x + y) & 0xff)) + y", 32),
        "part_sx": ("int32_t part_sx(int8_t x, int8_t y)", "(((x + y) & 0xff) ^ 0x80) - 0x80", 32),
    },
    "partial_i686.so": {
        "part32_al": ("uint8_t part32_al(uint8_t x, uint8_t y)", "x + y", 8),
        "part32_ah": ("uint32_t part32_ah(uint32_t x, uint32_t y)",
                      "(x & 0xffff00ff) | ((((x >> 8) + (y >> 8)) & 0xff) << 8)", 32),
    },
}
PARAMS = [(f, n) for f, cases in CASES.items() for n in cases]


@pytest.fixture(scope="module")
def views():
    opened = {}
    for file_name, cases in CASES.items():
        for d810 in (True, False):
            bv = open_sample(file_name, d810=d810)
            set_types(bv, {name: sig for name, (sig, _, _) in cases.items()})
            opened[(file_name, d810)] = bv
    yield opened
    for bv in opened.values():
        bv.file.close()


@pytest.mark.parametrize("file_name,name", PARAMS, ids=[n for _, n in PARAMS])
def test_partial_register_fixture(views, file_name, name):
    _, reference, width = CASES[file_name][name]
    expected = oracle.reference(reference, width)
    for d810 in (False, True):
        func = function_named(views[(file_name, d810)], name)
        final = oracle.function_result(func)
        result = oracle.compare(final, expected, width)
        assert result.kind in ("proven", "exhaustive"), (d810, name, result, "\n".join(map(str, func.hlil.instructions)))
    # The plugin removed the MBA (a rewrite in either pass). Dead partial stores it leaves behind,
    # e.g. result.w ^= y.w before result.w = x.w + y.w, are a presentation gap (byte-level liveness).
    func = function_named(views[(file_name, True)], name)
    assert any(r.rewrites for r in log.latest_reports(func).values()), "\n".join(map(str, func.hlil.instructions))
