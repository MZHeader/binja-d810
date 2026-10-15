import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "samples"))
import gen_mba  # noqa: E402

from binja_d810.ir.expr import is_mba, node_count  # noqa: E402
from binja_d810.ir.normalize import normalize  # noqa: E402
from binja_d810.rules.registry import load_rules  # noqa: E402
from binja_d810.semantic import cobra_shim  # noqa: E402
from binja_d810.semantic.backend import default_backends  # noqa: E402
from binja_d810.semantic.verify import Verdict, check_equivalent  # noqa: E402
from binja_d810.simplify import simplify_expression  # noqa: E402

SAMPLES = gen_mba.generate()


def test_corpus_has_every_category():
    # d810 count depends on how many proven rules are variable-only with a non-MBA replacement;
    # record the actual number in the Task 20 report.
    counts = Counter(s.category for s in SAMPLES)
    assert counts["d810"] >= 25 and counts["linear"] >= 40 and counts["poly"] == 2


def test_samples_are_correct_mbas():
    for s in SAMPLES:
        assert is_mba(normalize(s.obfuscated)), s.name
        assert check_equivalent(s.obfuscated, s.expected) is not Verdict.REFUTED, s.name


@pytest.mark.skipif(shutil.which("clang") is None, reason="clang missing")
def test_emitted_c_compiles(tmp_path):
    src = tmp_path / "mba.c"
    src.write_text(gen_mba.emit_c(SAMPLES))
    subprocess.run(["clang", "-arch", "arm64", "-fsyntax-only", "-Wall", "-Werror", str(src)], check=True)


def test_pipeline_simplifies_corpus():
    rules, backends = load_rules(), default_backends()
    failures = {"d810": [], "linear": [], "poly": []}
    for s in SAMPLES:
        out = simplify_expression(s.obfuscated, rules, backends)
        repl = out.rewrite.replacement if out.rewrite else None
        # Some targets mix arithmetic and bitwise ops themselves (x - (y & z)), so only demand a
        # non-MBA result when the expected answer is not one.
        demixed = not is_mba(repl) or is_mba(s.expected) if repl is not None else False
        ok = repl is not None and demixed and node_count(repl) <= node_count(s.expected) + 2
        if not ok:
            failures[s.category].append(s.name)
    assert failures["d810"] == []
    assert failures["linear"] == []
    if cobra_shim.load() is not None:
        assert failures["poly"] == []
