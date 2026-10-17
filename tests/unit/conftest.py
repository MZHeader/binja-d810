import pytest

from binja_d810.semantic import memo


@pytest.fixture(autouse=True)
def _forget_proofs():
    """Proofs are remembered process-wide by shape: each test starts without them,
    so a test that replaces the solver sees its own outcomes."""
    memo.clear()
    yield
    memo.clear()
