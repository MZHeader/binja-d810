import json
import pytest
from binja_d810.allocations.contracts import resolve


def spec(**options):
    return json.dumps(dict(version=1, role='allocate', parameter=0, fresh=True, ordinary=True, **options))


def test_imported_and_local_contracts_must_agree_exactly():
    declaration = spec(nonnull=True, elidable=True)
    assert resolve([None, declaration, declaration], 4096).elidable
    assert resolve([None, None], 4096) is None
    with pytest.raises(ValueError, match='conflicting'):
        resolve([declaration, spec(nonnull=False, elidable=False)], 4096)


def test_bad_library_metadata_cannot_fall_back_to_a_more_permissive_local_contract():
    with pytest.raises(ValueError): resolve([spec(nonnull=True), 'invalid'], 4096)
    with pytest.raises(ValueError, match='byte limit'): resolve([spec(nonnull=True)], 1)
