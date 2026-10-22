import pytest
from binja_d810.summaries.abi import red_zone


@pytest.mark.parametrize("platform, arch, size", (
    ("linux-x86_64", "x86_64", 128), ("mac-x86_64", "x86_64", 128),
    ("mac-aarch64", "aarch64", 128), ("ios-aarch64", "aarch64", 128),
    ("windows-x86_64", "x86_64", 0), ("linux-kernel-x86_64", "x86_64", 0),
    ("linux-x86_64", "x86", 0), ("unknown", "x86_64", 0),
))
def test_stack_reserve_is_an_abi_fact_with_an_explicit_cap(platform, arch, size):
    assert red_zone(platform, arch, 1024) == size
    assert red_zone(platform, arch, 32) == min(32, size)
    assert red_zone(platform, arch, 0) == 0
