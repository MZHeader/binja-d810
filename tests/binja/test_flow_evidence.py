"""v4.0: what the view knows for the flow fixes (binja_d810.binja.flow.evidence), on the arm64 -O2 flow build."""

import pytest
from flowcases import KEY
from helpers import data_pointers, function_named, open_sample

from binja_d810.binja.flow import evidence, lowlevel
from binja_d810.deadline import Deadline
from binja_d810.flow.uses import READ, classify


@pytest.fixture(scope="module")
def bv():
    view = open_sample("flow_O2.dylib", d810=False)
    yield view
    view.file.close()


def _table(bv, name: str) -> int:
    """The table a function reads: the lowest writable-data constant in its MLIL."""
    return data_pointers(bv, function_named(bv, name))[0]


def _never_written(bv, name: str):
    table = _table(bv, name)
    cache = {}

    def uses_of(func):
        if func.start not in cache:
            cache[func.start] = evidence.Uses(func.mlil.ssa_form)
        return cache[func.start]

    return evidence.never_written(bv, table, table + 16, uses_of)


def test_a_table_that_is_only_read_is_never_written(bv):
    assert _never_written(bv, "tbl_call_const") is None


def test_a_table_another_function_stores_to_is_written(bv):
    assert _never_written(bv, "written_call").startswith("write at")


def test_a_table_passed_to_a_call_escapes(bv):
    assert _never_written(bv, "escaped_call").startswith("escape at")


def test_the_use_graph_follows_a_table_address_to_its_load(bv):
    func = function_named(bv, "tbl_call_const")
    table = _table(bv, "tbl_call_const")
    uses = evidence.Uses(func.mlil.ssa_form)
    starts = uses.constants_in(table, table + 16)
    assert starts and classify(starts, uses) == (READ, None)


def test_missing_reference_lookups_scan_each_ssa_statement_once(bv):
    """Several folded-away references must not each traverse the same function's entire SSA again."""
    from binaryninja import MediumLevelILOperation as Op

    func = function_named(bv, "tbl_call_const")
    ssa = func.mlil.ssa_form

    class CountingSSA:
        def __init__(self):
            self.visited = 0

        def __getattr__(self, name):
            return getattr(ssa, name)

        @property
        def instructions(self):
            for statement in ssa.instructions:
                self.visited += 1
                yield statement

    constants = {node.expr_index: node.constant for statement in ssa.instructions
                 for node in statement.traverse(lambda n: n if n.operation in (Op.MLIL_CONST, Op.MLIL_CONST_PTR)
                                                else None)}
    assert constants
    counted = CountingSSA()
    uses = evidence.Uses(counted)
    for value in sorted(set(constants.values())):
        expected = {node for node, constant in constants.items() if value <= constant < value + 1}
        assert set(uses.constants_in(value, value + 1, at=bv.end + 0x1000)) == expected
    assert uses.constants_in(bv.end + 0x1000, bv.end + 0x1001, at=bv.end + 0x1000) == []
    assert counted.visited == len(list(ssa.instructions))


def test_the_code_view(bv):
    f_b = function_named(bv, "f_b")
    code = evidence.BinaryCode(bv, f_b.arch)
    assert code.is_code(f_b.start) and code.decodes(f_b.start) and code.function_at(f_b.start)
    assert f_b.start in code.owners(f_b.start)
    assert not code.is_code(_table(bv, "tbl_call_const"))
    assert not code.split_off(function_named(bv, "tbl_call_const").start, f_b.start)  # a named function


def test_the_reader(bv):
    read = evidence.reader(bv)
    table = _table(bv, "tbl_call_const")  # the table's start, or at -O2 the entry the function reads
    f_b = function_named(bv, "f_b").start
    assert any((read(a, 8) - KEY) & (2**64 - 1) == f_b for a in (table, table + 8))
    assert read(bv.end + 0x1000, 8) is None


def test_an_indexed_table_is_checked_from_its_base(bv):
    table = _table(bv, "tbl_call_const")
    assert evidence.object_start(bv, table + 8, base=table) == table
    assert evidence.object_start(bv, table + 8, base=table - 0x100000) != table - 0x100000


def test_types(bv):
    table = evidence.const_type(bv, 8, 2)
    assert table.width == 16 and evidence.is_const(table)
    again = evidence.parse_type(bv, evidence.type_text(table))
    assert evidence.type_text(again) == evidence.type_text(table)
    pointer = evidence.const_copy(bv, evidence.parse_type(bv, "void* __d810"))
    assert evidence.is_const(pointer) and not evidence.is_const(pointer.target)
    assert evidence.parse_type(bv, "not a type at all") is None


def test_an_entry_read_is_checked_from_a_pointer_to_its_table(bv):
    """escaped_call reads etab[1] at a constant address and passes etab to a call one word below."""
    cache = {}

    def uses_of(func):
        if func.start not in cache:
            cache[func.start] = evidence.Uses(func.mlil.ssa_form)
        return cache[func.start]

    table, entry = data_pointers(bv, function_named(bv, "escaped_call"))[:2]
    assert evidence.object_start(bv, entry, uses_of=uses_of) == table
    assert evidence.object_start(bv, entry) == entry


def test_an_unused_load_has_read_only_lowlevel_evidence(bv):
    func = function_named(bv, "key_read_unused")
    key = next(a for a in data_pointers(bv, function_named(bv, "tbl_jump_keyglobal"))
               if bv.read_int(a, 8, False) == KEY)
    refs = [r for r in bv.get_code_refs(key, 8) if r.function == func]
    assert refs, "the compiler must retain the unused load"
    uses = evidence.Uses(func.mlil.ssa_form)
    assert all(uses.reference_read_only(r.address, key, key + 8) for r in refs)


def test_lowlevel_reference_setup_does_not_scan_the_whole_function(bv):
    from types import SimpleNamespace

    func = function_named(bv, "escaped_offset")
    ssa = func.llil.ssa_form

    class CountingSSA:
        visited = 0

        def __getattr__(self, name):
            return getattr(ssa, name)

        @property
        def instructions(self):
            for statement in ssa.instructions:
                self.visited += 1
                yield statement

    counted = CountingSSA()

    class Function:
        llil = SimpleNamespace(ssa_form=counted)

        def __getattr__(self, name):
            return getattr(func, name)

    uses = lowlevel.Uses(Function())
    assert counted.visited == 0
    assert uses.has_return
    visited = counted.visited
    assert visited > 0 and uses.has_return and counted.visited == visited


def test_an_address_folded_to_a_later_entry_has_lowlevel_evidence(bv):
    func = function_named(bv, "tbl_call_const")
    target = function_named(bv, "f_b").start
    entry = next(a for a in data_pointers(bv, func) if bv.read_int(a, 8, False) - KEY == target)
    base = entry - 8
    refs = [r for r in bv.get_code_refs(base, 8) if r.function == func]
    assert refs, (hex(entry), data_pointers(bv, func), list(map(str, func.mlil.instructions)),
                  [(hex(i.address), str(i)) for i in func.llil.instructions])
    uses = evidence.Uses(func.mlil.ssa_form)
    assert not uses.constants_in(base, base + 8)
    assert all(uses.reference_read_only(r.address, base, base + 8) for r in refs)


def test_retyping_one_entry_does_not_refresh_readers_of_a_different_entry(bv):
    func = function_named(bv, "tbl_call_const")
    target = function_named(bv, "f_b").start
    entry = next(a for a in data_pointers(bv, func) if bv.read_int(a, 8, False) - KEY == target)
    base = entry - 8
    uses = {}

    def uses_of(f):
        if f.start not in uses:
            uses[f.start] = evidence.Uses(f.mlil.ssa_form)
        return uses[f.start]

    assert func.start in evidence.referencing(bv, base, base + 8)
    assert func.start not in evidence.affected_readers(bv, base, base, base + 8, uses_of, Deadline.after(5))
    assert func.start in evidence.affected_readers(bv, base, entry, entry + 8, uses_of, Deadline.after(5))
    assert func.start in evidence.affected_readers(bv, base, base, base + 8, uses_of, Deadline.after(0))


@pytest.mark.parametrize("name, owner", [("written_call", "written_store"), ("escaped_call", "escaped_call"),
                                        ("tbl_jump_var", "tbl_jump_var")])
def test_uncertain_or_overlapping_uses_still_need_refresh(bv, name, owner):
    first = _table(bv, name)
    func = function_named(bv, owner)
    uses = lambda f: evidence.Uses(f.mlil.ssa_form)
    assert func.start in evidence.affected_readers(bv, first, first, first + 16, uses, Deadline.after(5))


@pytest.mark.parametrize("name, owner", [("written_call", "written_store"), ("escaped_call", "escaped_call")])
def test_lowlevel_evidence_rejects_writes_and_escapes(bv, name, owner):
    first = _table(bv, name)
    func = function_named(bv, owner)
    refs = [r for r in bv.get_code_refs(first, 8) if r.function == func]
    assert refs
    assert all(not lowlevel.Uses(func).read_only(r.address, first, first + 8) for r in refs)


def test_lowlevel_evidence_rejects_a_returned_offset_pointer(bv):
    from binaryninja import LowLevelILOperation as L, MediumLevelILOperation as Op

    func = function_named(bv, "escaped_offset")
    entry = next(i.src[0].value.value for i in func.mlil.instructions if i.operation == Op.MLIL_RET)
    base = entry - 8
    constructors = [i for i in func.llil.instructions
                    if i.operation == L.LLIL_SET_REG and i.src.value.value == base]
    assert constructors
    assert all(not lowlevel.Uses(func).read_only(i.address, base, base + 8) for i in constructors)
