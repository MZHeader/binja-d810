"""v4.0: what the view knows for the flow fixes (binja_d810.binja.flow.evidence), on the arm64 -O2 flow build."""

import pytest
from flowcases import KEY
from helpers import data_pointers, function_named, open_sample

from binja_d810.binja.flow import evidence
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
