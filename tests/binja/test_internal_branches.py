"""Machine jumps to a block in the same function, even when MLIL calls them tail calls."""

import pytest
from binaryninja import LowLevelILOperation as L, MediumLevelILOperation as M, Settings
from flowcases import text
from helpers import function_named, open_sample, settle

from binja_d810.binja import settings, workflow
from binja_d810.binja.flow import apply, collect, evidence
from binja_d810.deadline import Deadline

BUILDS = ["internal_branches.dylib", "internal_branches_x86_64.so",
          "internal_branches_x86_64-pe.dll", "internal_branches_i686-pe.dll"]


@pytest.fixture(scope="module", params=BUILDS)
def builds(request):
    off = open_sample(request.param, d810=False)
    on = open_sample(request.param, d810=True)
    settle(on)
    yield on, off
    on.file.close()
    off.file.close()


def _indirects(func):
    return [i for i in func.mlil.instructions
            if i.operation in (M.MLIL_JUMP, M.MLIL_CALL, M.MLIL_CALL_UNTYPED,
                               M.MLIL_TAILCALL, M.MLIL_TAILCALL_UNTYPED)]


def _branches(bv, func):
    return [c for c in apply.record_of(bv).changes()
            if c.kind == "branches" and c.detail["function"] == func.start]


def _site(bv, name):
    return next(site for site, _ in collect.scan(function_named(bv, name).mlil, Deadline.after(5), evidence.reader(bv)))


def test_a_machine_jump_into_the_function_is_recovered(builds):
    on, off = builds
    before = function_named(off, "internal_branch")
    sites = _indirects(before)
    assert sites, text(before)
    assert any(s.operation in (M.MLIL_TAILCALL, M.MLIL_TAILCALL_UNTYPED) for s in sites), text(before)
    assert any(i.operation == L.LLIL_JUMP for s in sites for i in before.get_lifted_ils_at(s.address))
    func = function_named(on, "internal_branch")
    assert not _indirects(func), text(func)
    assert "+ 3" in text(func), text(func)


@pytest.mark.parametrize("name", ["internal_call", "written_internal_branch"])
def test_real_calls_and_written_targets_stay(builds, name):
    on, off = builds
    func = function_named(on, name)
    assert _indirects(func) and not _branches(on, func), text(func)
    assert text(func) == text(function_named(off, name))


@pytest.mark.parametrize("file", BUILDS)
def test_disabling_restores_the_internal_jump(file):
    bv = open_sample(file, d810=True)
    off = open_sample(file, d810=False)
    try:
        settle(bv)
        func = function_named(bv, "internal_branch")
        assert not _indirects(func), text(func)
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, bv)
        for f in bv.functions:
            f.reanalyze()
        settle(bv)
        assert not apply.record_of(bv).changes()
        assert text(function_named(bv, "internal_branch")) == text(function_named(off, "internal_branch"))
    finally:
        bv.file.close()
        off.file.close()


@pytest.mark.parametrize("file", BUILDS)
def test_clearing_a_trusted_range_restores_the_mutable_jump(file):
    bv = open_sample(file, d810=False)
    try:
        func = function_named(bv, "written_internal_branch")
        before = text(func)
        load = _site(bv, "written_internal_branch").target.loads[0]
        at = load.addresses[0]
        Settings().set_string_list(settings.READ_ONLY_RANGES, [f"{at:#x}-{at + load.size - 1:#x}"], bv)
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        assert not _indirects(func) and _branches(bv, func), text(func)
        Settings().set_string_list(settings.READ_ONLY_RANGES, [], bv)
        func.reanalyze()
        settle(bv)
        assert text(func) == before and not _branches(bv, func), text(func)
    finally:
        bv.file.close()


@pytest.mark.parametrize("file", [f for f in BUILDS if f.endswith(".dll")])
def test_editing_the_pointer_discards_the_old_branch_target(file):
    # ELF/Mach-O relocations override a raw file-byte patch here; PE exposes the changed pointer value.
    bv = open_sample(file, d810=False)
    try:
        func = function_named(bv, "internal_branch")
        load = _site(bv, "internal_branch").target.loads[0]
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze()
        settle(bv)
        assert not _indirects(func) and _branches(bv, func), text(func)
        assert bv.write(load.addresses[0], bytes(load.size), except_on_relocation=False) == load.size
        assert evidence.reader(bv)(load.addresses[0], load.size) == 0
        func.reanalyze()
        settle(bv)
        assert not _branches(bv, func), apply.record_of(bv).to_json()
    finally:
        bv.file.close()


@pytest.mark.parametrize("file", BUILDS)
def test_user_branch_targets_are_kept(file):
    bv = open_sample(file, d810=True)
    try:
        settle(bv)
        func = function_named(bv, "internal_branch")
        (change,) = _branches(bv, func)
        func.set_user_indirect_branches(change.key, [(func.arch, func.start)])
        for enabled in (True, False):
            Settings().set_bool(workflow.ACTIVITY_SETTING, enabled, func)
            func.reanalyze()
            settle(bv)
            branches = func.get_indirect_branches_at(change.key)
            assert len(branches) == 1 and not branches[0].auto_defined, branches
    finally:
        bv.file.close()


@pytest.mark.parametrize("file", BUILDS)
def test_changing_the_instruction_to_a_call_discards_the_edge(file):
    bv = open_sample(file, d810=True)
    try:
        settle(bv)
        func = function_named(bv, "internal_branch")
        (change,) = _branches(bv, func)
        call = next(i for i in function_named(bv, "internal_call").lifted_il.instructions
                    if i.operation == L.LLIL_CALL)
        code = bv.read(call.address, func.arch.max_instr_length)
        size = func.arch.get_instruction_info(code, call.address).length
        assert bv.write(change.key, code[:size]) == size  # only this synthetic view is patched
        func.reanalyze()
        settle(bv)
        assert not _branches(bv, func), apply.record_of(bv).to_json()
    finally:
        bv.file.close()
