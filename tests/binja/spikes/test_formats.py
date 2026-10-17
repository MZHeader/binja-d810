"""PE/ELF formats spike: load each cross-built corpus, find every sample."""

import json
import os
from pathlib import Path

import binaryninja
import pytest
from helpers import manifest

DIR = Path(os.environ.get("BINJA_D810_FORMATS_DIR", "."))
OUT = Path(os.environ.get("BINJA_D810_PYTEST_OUT", ".binja-test"))
FILES = sorted(p.name for p in DIR.glob("mba_*")) if DIR.is_dir() else []


@pytest.mark.parametrize("name", [f for f in FILES if f.endswith((".dll", ".so"))])
def test_loads_and_finds_every_sample(name):
    bv = binaryninja.load(str(DIR / name), options={"extension.binja_d810.mba": False})
    try:
        missing = []
        for entry in manifest():
            if not any(bv.get_functions_by_name(n) for n in (entry["name"], "_" + entry["name"])):
                missing.append(entry["name"])
        record = {"arch": str(bv.arch), "platform": str(bv.platform), "view": bv.view_type,
                  "functions": len(bv.functions), "missing": missing,
                  "sample_mlil": [str(i) for i in bv.get_functions_by_name(manifest()[0]["name"])[0].mlil.instructions]
                  if not missing else None}
        out = OUT / "formats.json"
        data = json.loads(out.read_text()) if out.exists() else {}
        data[name] = record
        out.write_text(json.dumps(data, indent=1))
        assert missing == [], missing
    finally:
        bv.file.close()
