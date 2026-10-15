from pathlib import Path

import binja_d810

SHA = "2e59ab4ce18e5e82911880137d1285d95279981a"
ROOT = Path(binja_d810.__file__).parent / "third_party" / "d810"
VENDORED = [
    "core/typing.py", "core/registry.py", "core/bits.py",
    "mba/dsl.py", "mba/constraints.py", "mba/backends/z3.py",
    *(f"mba/rules/{n}.py" for n in (
        "__init__", "_base", "add", "and_", "bnot", "cst", "hodur", "misc",
        "mov", "mul", "neg", "or_", "predicates", "sub", "xor",
    )),
]


def test_vendored_files_carry_provenance_header():
    for rel in VENDORED:
        text = (ROOT / rel).read_text()
        assert text.startswith(f"# Vendored from https://github.com/w00tzenheimer/d810-ng @ {SHA}"), rel


def test_no_imports_of_the_real_d810_package():
    for path in ROOT.rglob("*.py"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("from d810", "import d810")), f"{path}: {line}"
            assert 'import_module("d810.' not in stripped, f"{path}: {line}"


def test_rules_import_and_register():
    from binja_d810.third_party.d810.mba.rules import VerifiableRule

    names = {type(r).__name__ for r in VerifiableRule.instantiate_all()}
    assert "Xor_HackersDelightRule_1" in names
    assert len(names) >= 150


def test_dsl_constraints_find_core_helpers():
    # when.equals_minus_two imports AND_TABLE from d810.core lazily, at match time.
    from types import SimpleNamespace

    from binja_d810.third_party.d810.mba.dsl import when

    check = when.equals_minus_two("c")
    assert check({"c": SimpleNamespace(value=0xFFFFFFFE, size=4)}) is True
    assert check({"c": SimpleNamespace(value=0xFFFFFFFD, size=4)}) is False


def test_vendored_z3_backend_proves_a_rule():
    from binja_d810.third_party.d810.mba.backends.z3 import verify_rule
    from binja_d810.third_party.d810.mba.rules import VerifiableRule

    rule = next(r for r in VerifiableRule.instantiate_all() if type(r).__name__ == "Xor_HackersDelightRule_1")
    assert verify_rule(rule, bit_width=32) is True
