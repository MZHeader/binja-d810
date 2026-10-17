"""v3 spike: comparison shapes (S1), constant conditions and dead branches (S2), what survives -O2
(S3). Evidence for docs/design/spikes/2026-10-17-v3-predicates.md.

Run: BINJA_D810_SPIKE=1 scripts/binja_pytest.sh tests/binja/spikes/test_v3_spike.py
"""

import json
import os
from pathlib import Path

import pytest
import v3_spike
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, set_types

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test")) / "spike"
NAMES = ("opaque_parity", "opaque_setnz", "opaque_mba", "plain_condition", "mod2")
TYPES = {n: f"uint32_t {n}(uint32_t x, uint32_t y)" for n in NAMES}
MODES = ("const_size0_true", "const_size1_true", "const_size4_true", "const_size1_false", "cmp_true", "cmp_false")
EVIDENCE: dict = {}


def _tree(e):
    if not hasattr(e, "operation"):
        return repr(e)
    inner = ", ".join(_tree(k) for k in e.operands)
    return f"{e.operation.name}/{e.size}({inner})"


def _blocks(func):
    return [(bb.start, bb.end, func.mlil[bb.end - 1].operation.name) for bb in func.mlil.basic_blocks]


@pytest.fixture(autouse=True, scope="module")
def _registered():
    v3_spike.register()
    yield
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "evidence.json").write_text(json.dumps(EVIDENCE, indent=1, default=str))


@pytest.mark.parametrize("build,level", MATRIX, ids=IDS)
def test_spike(build, level):
    key = f"{build}-{level}"
    bv = open_sample(sample("predicates", build, level), d810=False)
    try:
        set_types(bv, TYPES)
        out = OUT / key
        out.mkdir(parents=True, exist_ok=True)
        for name in NAMES:  # S1 and S3: shapes with the plugin off
            func = function_named(bv, name)
            lines = ["== mlil ssa"] + [f"{i.instr_index} {i}\n    {_tree(i)}" for i in func.mlil.ssa_form.instructions]
            lines += ["== hlil"] + [f"{i}\n    {_tree(i)}" for i in func.hlil.instructions]
            (out / f"{name}.txt").write_text("\n".join(lines))
        func = function_named(bv, "opaque_parity")
        baseline = _blocks(func)
        results = {}
        for mode in MODES:  # S2: constant conditions
            v3_spike.MODES.clear()
            v3_spike.MODES["opaque_parity"] = mode
            func.reanalyze()
            bv.update_analysis_and_wait()
            func = function_named(bv, "opaque_parity")
            results[mode] = {"hlil": [str(i) for i in func.hlil.instructions], "blocks_same": _blocks(func) == baseline,
                             "mlil_if": [str(i) for i in func.mlil.instructions if i.operation.name == "MLIL_IF"]}
            func.reanalyze()  # stable on a second analysis?
            bv.update_analysis_and_wait()
            func = function_named(bv, "opaque_parity")
            results[mode]["hlil_again"] = [str(i) for i in func.hlil.instructions]
        v3_spike.MODES.clear()
        EVIDENCE[key] = {"modes": results, "log": {hex(k): v for k, v in v3_spike.LOG.items()}}
    finally:
        bv.file.close()
