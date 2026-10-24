"""Closed mutable domains, exact invariant proofs, atomic writes and independent reads."""
from dataclasses import replace
import pytest
from binaryninja import BinaryView, ILInstructionAttribute as Attr, MediumLevelILFunction, Settings
from arch import IDS, MATRIX, sample
from helpers import function_named, global_reads, lift_return, open_sample, set_types, settle
from flowcases import text
from binja_d810.binja import log, mutable_values, settings, workflow
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import Const

INVARIANTS = {
    'mv_invariant': (0x34, {0x1234, 0x1274, 0x80001234, 0x80001274}),
    'mv_atomic_invariant': (0x23, {0x5623, 0x5663}),
    'mv_atomic_add_invariant': (0x9876, {0x9876, 0x80009876}),
    'mv_zero_invariant': (0, {0, 0x40}),
    'mv_partial_invariant': (0x123400aa, {0x123400aa | (b << 8) for b in (0, 0x40, 0x80, 0xc0)}),
    'mv_atomic_partial_invariant': (0x567800bb, {0x567800bb | (b << 8) for b in (0, 0x40, 0x80, 0xc0)}),
    'mv_word_invariant': (0x9a3400ab, {0x9a3400ab | (b << 8) for b in (0, 0x40, 0x80, 0xc0)}),
    'mv_atomic_word_invariant': (0xdefc118a, {0xdefc918a, 0xdefc118a}),
}
NEGATIVES = ('mv_unknown_read', 'mv_escaped_read', 'mv_exported_read', 'mv_exported_partial', 'mv_large_read')
WRITERS = ('mv_update', 'mv_atomic_update', 'mv_atomic_add', 'mv_unknown_update', 'mv_escape', 'mv_zero_update', 'mv_large_update',
           'mv_partial_update', 'mv_atomic_partial_update', 'mv_word_update', 'mv_atomic_word_update')


@pytest.fixture(scope='module', params=MATRIX, ids=IDS)
def builds(request):
    on = open_sample(sample('mutable_values', *request.param), d810=True)
    off = open_sample(sample('mutable_values', *request.param), d810=True, options={settings.MUTABLE_VALUES: False})
    for bv in (on, off):
        types = {name: f'uint32_t {name}(void)' for name in (*INVARIANTS, *NEGATIVES, 'mv_varying', 'mv_two_reads', 'mv_partial_varying')}
        types.update({name: f'void {name}(uint32_t x)' for name in WRITERS if name != 'mv_escape'})
        types['mv_escape'] = 'void mv_escape(void)'
        set_types(bv, types); settle(bv)
    yield request.param, on, off
    on.file.close(); off.file.close()


@pytest.mark.parametrize('name', INVARIANTS)
def test_invariants_fold_but_memory_reads_and_writes_remain(builds, name):
    _, on, off = builds
    func, before = function_named(on, name), function_named(off, name)
    expected, domain = INVARIANTS[name]
    report = log.latest_report(func)
    assert report is not None, (func.name, func.analysis_skip_reason, func.analysis_performance_info)
    assert report.mutable_folds > 0, log.render(report)
    assert lift_return(func) == Const(expected, 32), text(func)
    assert any(set(values) == domain and size == 4 for _, size, values in report.mutable_domains)
    assert global_reads(func) == global_reads(before) and global_reads(func)
    assert 'skipped constant key: write at' in log.render(report)
    assert 'D810 possible global values:' in func.get_comment_at(func.start)
    for address, _, _ in report.mutable_domains:
        assert str(on.get_data_var_at(address).type) == str(off.get_data_var_at(address).type)


@pytest.mark.parametrize('name', WRITERS)
def test_atomic_and_other_writer_bodies_are_unchanged(builds, name):
    _, on, off = builds
    assert text(function_named(on, name)) == text(function_named(off, name))


@pytest.mark.parametrize('name', NEGATIVES)
def test_unknown_writers_escaped_and_exported_globals_stay_excluded(builds, name):
    _, on, off = builds
    func = function_named(on, name)
    report = log.latest_report(func)
    assert not report.mutable_folds and not report.mutable_domains
    assert text(func) == text(function_named(off, name))


@pytest.mark.parametrize('name', ('mv_varying', 'mv_two_reads', 'mv_partial_varying'))
def test_varying_reads_are_reported_without_equating_distinct_observations(builds, name):
    _, on, off = builds
    func = function_named(on, name)
    report = log.latest_report(func)
    assert report.mutable_domains and not report.mutable_folds
    assert text(func) == text(function_named(off, name))
    assert not isinstance(lift_return(func), Const)


@pytest.mark.parametrize('limit', ('instructions', 'work', 'expressions', 'readers', 'values', 'writes', 'closure_steps', 'bits', 'evidence_bytes'))
def test_caps_and_deadlines_never_commit_a_partial_proof(builds, limit):
    _, _, off = builds
    il = function_named(off, 'mv_invariant').mlil
    new, report = mutable_values.run(il, replace(mutable_values.Limits(), **{limit: 1}), Deadline.after(2))
    assert new is il and not report.folded
    new, report = mutable_values.run(il, deadline=Deadline.after(0))
    assert new is il and not report.folded and report.exhausted
    new, report = mutable_values.run(il, replace(mutable_values.Limits(), bits=128))
    assert new is il and not report.folded and '64 bits' in report.notes[0]


@pytest.mark.parametrize('build', MATRIX, ids=IDS)
def test_disabling_restores_consumers_and_comments(build):
    bv = open_sample(sample('mutable_values', *build), d810=True, options={settings.MUTABLE_VALUES: False})
    try:
        func = function_named(bv, 'mv_invariant')
        before = text(func)
        Settings().set_bool(settings.MUTABLE_VALUES, True, bv)
        for f in bv.functions: f.reanalyze()
        settle(bv)
        assert log.latest_report(func).mutable_folds
        owned = func.get_comment_at(func.start); assert owned
        func.set_comment_at(func.start, 'my note\n' + owned)
        Settings().set_bool(settings.MUTABLE_VALUES, False, bv)
        for f in bv.functions: f.reanalyze()
        settle(bv)
        assert text(func) == before and func.get_comment_at(func.start) == 'my note'
    finally: bv.file.close()


def test_settings_and_native_comment_undo():
    for field, (name, default) in settings.MUTABLE_LIMITS.items():
        assert Settings().get_integer(f'{settings.PREFIX}.{name}') == default == getattr(mutable_values.Limits(), field)
    bv = open_sample('mutable_values_O2.dylib', d810=False)
    try:
        func = function_named(bv, 'mv_invariant')
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze(); settle(bv)
        assert func.get_comment_at(func.start)
        bv.undo(); assert func.get_comment_at(func.start) == ''
        func.reanalyze(); settle(bv)
        assert func.get_comment_at(func.start) == ''
        Settings().set_bool(settings.MUTABLE_COMMENTS, False, bv)
        func.reanalyze(); settle(bv)
        assert log.latest_report(func).mutable_domains and func.get_comment_at(func.start) == ''
    finally: bv.file.close()


@pytest.mark.parametrize('mode', ('protected', 'repeated_load'))
def test_protected_and_repeated_raw_loads_cannot_be_folded(builds, mode):
    _, _, off = builds
    func = function_named(off, 'mv_invariant')
    _, baseline = mutable_values.run(func.mlil)
    address, _, _ = baseline.domains[0]
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(func.mlil); il.prepare_to_copy_block(func.mlil.basic_blocks[0])
    il.set_current_address(func.start, func.arch)
    load = il.load(4, il.const_pointer(func.arch.address_size, address))
    if mode == 'protected':
        il.set_expr_attributes(load, {Attr.ILPreventDeadStoreElimination})
        result = il.and_expr(4, load, il.const(4, 0x3f))
    else:
        result = il.xor_expr(4, load, load)
    il.append(il.ret([result])); il.finalize(); il.generate_ssa_form()
    new, report = mutable_values.run(il)
    assert new is il and not report.folded
    assert any('protected' in note or 'repeated raw load' in note for note in report.notes)


def test_initial_cross_reference_discovery_is_required(builds, monkeypatch):
    _, _, off = builds
    il = function_named(off, 'mv_invariant').mlil
    monkeypatch.setattr(BinaryView, 'has_initial_analysis', lambda _: False)
    new, report = mutable_values.run(il)
    assert new is il and report.deferred and not report.folded and not report.domains


@pytest.mark.parametrize('flow_enabled', (False, True))
def test_initial_analysis_automatically_retries_after_writer_discovery(flow_enabled, monkeypatch):
    observed = []
    original = mutable_values.run
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append((result[1].deferred, result[1].folded))
        return result
    monkeypatch.setattr(mutable_values, 'run', capture)
    bv = open_sample('mutable_values_O2.dylib', d810=True, options={settings.FLOW_FIXES: flow_enabled})
    try:
        settle(bv)
        report = log.latest_report(function_named(bv, 'mv_invariant'))
        assert report.mutable_folds and report.mutable_domains, log.render(report)
        assert any(deferred for deferred, _ in observed)
        assert any(folded for _, folded in observed)
    finally: bv.file.close()
