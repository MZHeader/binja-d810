import binja_d810


def test_package_imports_without_binary_ninja():
    assert binja_d810.binaryninja is None
