"""The build matrix (spec section 9.1): every target at -O0 and -O2.

Select one architecture with pytest's -k, e.g. scripts/binja_pytest.sh tests/binja -k x86_64-pe.
"""

BUILDS = ("arm64", "x86_64-pe", "i686-pe", "x86_64-elf")
LEVELS = ("O0", "O2")
MATRIX = [(build, level) for build in BUILDS for level in LEVELS]
IDS = [f"{build}-{level}" for build, level in MATRIX]


def sample(base: str, build: str, level: str) -> str:
    """File name in tests/samples/build. ARM64 keeps v1's names."""
    if build == "arm64":
        return f"{base}_{level}.dylib"
    return f"{base}_{build}_{level}{'.dll' if build.endswith('-pe') else '.so'}"


# Expected misses outside the corpus test, as (test, build-level) -> specific technical reason. Strict:
# a listed case that starts passing fails the run and must be removed from this table.
_ALIASED = ("the address-taken value is reloaded into a register that is reused before the combining "
            "instruction, and HLIL reads the address-taken variable itself, an aliased read the effect "
            "policy rejects (spec 5.2)")
MISSES: dict[tuple[str, str], str] = {
    ("eff_aliased", "x86_64-pe-O0"): _ALIASED,
    ("eff_aliased", "i686-pe-O0"): _ALIASED,
    ("eff_aliased", "i686-pe-O2"): _ALIASED,
    ("eff_aliased", "x86_64-elf-O0"): _ALIASED,
}


def expect_miss(request, test: str, build_level: str) -> None:
    import pytest

    reason = MISSES.get((test, build_level))
    if reason is not None:
        request.applymarker(pytest.mark.xfail(reason=reason, strict=True))
