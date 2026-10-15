import json
import os
from pathlib import Path

import binaryninja
import hlil_spike
import oracle
from binaryninja import HighLevelILOperation as H, Settings
from helpers import BUILD, function_named, manifest, set_types, signature

OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test"))
REUSE = ["mba_linear_001", "mba_linear_002", "mba_linear_006", "mba_linear_013", "mba_linear_030"]


def _ssa_reads(func):
    out = []
    for instr in func.hlil.ssa_form.instructions:
        stack = [instr]
        while stack:
            n = stack.pop()
            if isinstance(n, list):
                stack.extend(n)
                continue
            if not hasattr(n, "operation"):
                continue
            if n.operation == H.HLIL_VAR_SSA:
                d = func.hlil.ssa_form.get_ssa_var_definition(n.var)
                out.append((str(instr), n.var.var.name, n.var.version, None if d is None else str(d),
                            n.var.var in func.parameter_vars))
            stack.extend(n.operands)
    return out


def test_explore():
    hlil_spike.register()
    entries = {e["name"]: e for e in manifest()}
    report = {}
    bv = binaryninja.load(str(BUILD / "mba_O2.dylib"), options={hlil_spike.NAME: True})
    set_types(bv, {n: signature(entries[n]) for n in REUSE})
    for name in REUSE:
        func = function_named(bv, name)
        r = {"log": hlil_spike.LOG.get(func.start, [])[-1:],
             "hlil": [str(i) for i in func.hlil.instructions],
             "mlil": [str(i) for i in func.mlil.instructions],
             "pseudo_c": [str(l) for l in func.pseudo_c.get_linear_lines(func.hlil.root)] if hasattr(func.pseudo_c, "get_linear_lines") else None,
             "ssa_reads": _ssa_reads(func)}
        try:
            final = oracle.function_result(func)
            r["oracle"] = str(oracle.compare(final, oracle.reference(entries[name]["expected"], entries[name]["width"]), entries[name]["width"]))
        except Exception as exc:
            r["oracle"] = repr(exc)
        report[name] = r
    (OUT / "explore.json").write_text(json.dumps(report, indent=1, default=str))
