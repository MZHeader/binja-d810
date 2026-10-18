"""SSA definitions by storage and version (v3.3): Binary Ninja numbers versions per storage location,
and after rewrites a read can name another variable of the same register than its definition."""

from types import SimpleNamespace as NS

from binja_d810.binja.ssa import definition_of

EDX = 2  # a register's storage


def var(name, identifier, storage=EDX, source_type=1):
    return NS(name=name, identifier=identifier, storage=storage, source_type=source_type)


def ssa(v, version):
    return NS(var=v, version=version)


class FakeSSA:
    """x-tunnel.bin 0x424760: if (edx_98#97 != ...) reads version 97 of edx, which edx_103#97 = phi(...) defines."""

    def __init__(self):
        self.edx_98, self.edx_103, self.eax = var("edx_98", 1), var("edx_103", 2), var("eax", 3, storage=0)
        self.phi = NS(text="edx_103#97 = phi(edx_98#90, edx_98#96)", vars_written=[ssa(self.edx_103, 97)])
        self.one = NS(text="eax#4 = 1", vars_written=[ssa(self.eax, 4)])
        self.instructions = [NS(text="edx_98#90 = 5", vars_written=[ssa(self.edx_98, 90)]), self.phi, self.one]
        self.lookups = 0

    @property
    def ssa_vars(self):
        raise AssertionError("every SSA version of every variable: one Binary Ninja call per variable")

    def get_ssa_var_definition(self, v):
        self.lookups += 1
        if v.var is self.edx_103 and v.version == 97:
            return self.phi
        if v.var is self.eax and v.version == 4:
            return self.one
        return None


def test_a_definition_filed_under_another_variable_of_the_storage():
    f = FakeSSA()
    cache: dict = {}
    assert definition_of(f, ssa(f.edx_98, 97), cache) == f.phi
    assert definition_of(f, ssa(f.eax, 4), cache) == f.one


def test_no_definition_stays_none():
    f = FakeSSA()
    assert definition_of(f, ssa(f.edx_98, 0), {}) is None  # version 0: an input
    assert definition_of(f, ssa(f.edx_98, 55), {}) is None  # nothing of the storage at that version
    other_storage = var("ecx_1", 9, storage=1)
    assert definition_of(f, ssa(other_storage, 97), {}) is None


def test_lookups_are_remembered():
    f = FakeSSA()
    cache: dict = {}
    for _ in range(3):
        definition_of(f, ssa(f.edx_98, 97), cache)
    assert f.lookups == 1  # the variable's own lookup; the storage's comes from one scan


def test_without_a_shared_cache_there_is_no_storage_index():
    f = FakeSSA()
    assert definition_of(f, ssa(f.edx_98, 97), {}, by_storage=False) is None
