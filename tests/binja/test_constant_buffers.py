"""Randomized C fixtures: exact recovered bytes, regenerated IL, comments and undo."""
import json
from dataclasses import replace
import pytest
from binaryninja import (ILInstructionAttribute as Attr, MediumLevelILFunction, Settings, Variable, VariableSourceType)
from arch import IDS, MATRIX, sample
from helpers import BUILD, function_named, open_sample, set_types, settle
from flowcases import text
from buffer_model import recover
from binja_d810.binja import constant_buffers, flag_conditions, log, settings, workflow
from binja_d810.binja.flow import apply
from binja_d810.deadline import Deadline

CASES = json.loads((BUILD / "constant_buffers.json").read_text())


@pytest.fixture(scope="module", params=MATRIX, ids=IDS)
def builds(request):
    on = open_sample(sample("constant_buffers", *request.param), d810=True)
    off = open_sample(sample("constant_buffers", *request.param), d810=True, options={settings.CONSTANT_BUFFERS: False})
    for bv in (on, off):
        types = {c['name']: f"void {c['name']}(void)" for c in CASES}
        types.update({name: f"void {name}(void)" for name in ('ce_length', 'ce_writable', 'ce_call', 'ce_volatile')})
        types.update({name: f"void {name}(uint32_t x)" for name in ('ce_unknown', 'ce_reentry')})
        set_types(bv, types)
        settle(bv)
    yield request.param, on, off
    on.file.close(); off.file.close()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c['name'])
def test_randomized_strings_and_final_il_match_the_independent_generator(builds, case, request):
    build, on, off = builds
    func = function_named(on, case['name'])
    report = log.latest_report(func)
    expected = bytes(case['plaintext'])
    if case['name'] == 'ce_unrolled_003' and build in (('x86_64-pe', 'O2'), ('x86_64-elf', 'O2'), ('i686-pe', 'O2')):
        baseline = recover(function_named(off, case['name']))
        assert baseline and expected not in baseline
        assert recover(func) == baseline
        request.applymarker(pytest.mark.xfail(strict=True, reason='Native constant-copy data already has an incorrect byte before D810; on/off bytes agree'))
    assert expected[:-1].decode('ascii') in [value for _, value in report.constant_strings], log.render(report)
    assert expected in recover(func), text(func)
    assert report.constant_regions == 1 and report.constant_steps > 0
    for address, value in report.constant_strings:
        assert json.dumps(value) in func.get_comment_at(address)


def test_length_delimited_buffer_and_phi_flag_regression(builds):
    _, on, off = builds
    func = function_named(on, 'ce_length')
    assert 'ABCDEFG' in [s for _, s in log.latest_report(func).constant_strings]
    assert b'ABCDEFG' in recover(func)
    # Phi.src is a list. The flag pass must not treat it as one overflow expression.
    from binja_d810.flags.recombine import Limits
    flag_conditions.run(function_named(off, 'ce_reentry').hlil, 'hlil', Limits(), Deadline.after(5))


@pytest.mark.parametrize('name', ('ce_unknown', 'ce_writable', 'ce_call', 'ce_reentry', 'ce_volatile'))
def test_unknown_inputs_effects_and_reentry_never_produce_a_string(builds, name):
    _, on, off = builds
    report = log.latest_report(function_named(on, name))
    assert not report.constant_strings
    assert text(function_named(on, name)) == text(function_named(off, name))


@pytest.mark.parametrize("limit", ("instructions", "work", "frame_bytes", "steps", "trips"))
def test_limits_and_expired_budget_leave_the_original_il(builds, limit):
    _, _, off = builds
    il = function_named(off, "ce_loop_000").mlil
    new, report = constant_buffers.run(il, replace(constant_buffers.Limits(), **{limit: 1}), Deadline.after(2))
    assert new is il and not report.regions and report.notes
    new, report = constant_buffers.run(il, deadline=Deadline.after(0))
    assert new is il and not report.regions and report.exhausted


@pytest.mark.parametrize("build", MATRIX, ids=IDS)
def test_disabling_restores_il_and_user_comments(build):
    bv = open_sample(sample("constant_buffers", *build), d810=True, options={settings.CONSTANT_BUFFERS: False})
    try:
        func = function_named(bv, "ce_loop_000")
        set_types(bv, {"ce_loop_000": "void ce_loop_000(void)"}); settle(bv)
        before = text(func)
        Settings().set_bool(settings.CONSTANT_BUFFERS, True, bv)
        for f in bv.functions: f.reanalyze()
        settle(bv)
        report = log.latest_report(func); assert report.constant_strings
        at, _ = report.constant_strings[0]
        owned = func.get_comment_at(at)
        func.set_comment_at(at, "my note\n" + owned + "\nkeep this too")
        Settings().set_bool(settings.CONSTANT_BUFFERS, False, bv)
        for f in bv.functions: f.reanalyze()
        settle(bv)
        assert func.get_comment_at(at) == "my note\nkeep this too"
        assert text(func) == before
        assert not any(c.kind.startswith('comment:') for c in apply.record_of(bv).changes())
    finally: bv.file.close()


def test_native_undo_and_comment_opt_out():
    bv = open_sample("constant_buffers_O2.dylib", d810=False)
    try:
        func = function_named(bv, "ce_loop_000")
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze(); settle(bv)
        at, _ = log.latest_report(func).constant_strings[0]
        assert func.get_comment_at(at).startswith('D810 buffer text:')
        bv.undo()
        assert func.get_comment_at(at) == ''
        func.reanalyze(); settle(bv)
        assert func.get_comment_at(at) == ''
        Settings().set_bool(settings.CONSTANT_COMMENTS, False, bv)
        func.reanalyze(); settle(bv)
        assert log.latest_report(func).constant_strings and func.get_comment_at(at) == ''
    finally: bv.file.close()


@pytest.mark.parametrize('key', (settings.ENABLED, settings.FLOW_FIXES, settings.CONSTANT_COMMENTS))
def test_comment_gates_remove_owned_text_and_keep_user_edits(key):
    bv = open_sample('constant_buffers_O2.dylib', d810=False)
    try:
        func = function_named(bv, 'ce_loop_000')
        Settings().set_bool(workflow.ACTIVITY_SETTING, True, func)
        func.reanalyze(); settle(bv)
        at, _ = log.latest_report(func).constant_strings[0]
        owned = func.get_comment_at(at)
        assert owned
        func.set_comment_at(at, 'user replacement')
        func.reanalyze(); settle(bv)
        assert func.get_comment_at(at) == 'user replacement'
        # Restore the exact owned line beside a user line to exercise selective removal.
        func.set_comment_at(at, owned + '\nuser replacement')
        Settings().set_bool(key, False, bv)
        func.reanalyze(); settle(bv)
        assert func.get_comment_at(at) == 'user replacement'
    finally: bv.file.close()


def test_settings_match_the_evaluator_defaults():
    preferences, limits = Settings(), constant_buffers.Limits()
    for field, (name, default) in settings.CONSTANT_LIMITS.items():
        assert preferences.get_integer(f'{settings.PREFIX}.{name}') == default == getattr(limits, field)


@pytest.mark.parametrize('attribute', (Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis))
def test_protected_native_memory_writes_are_retained(builds, attribute):
    _, _, off = builds
    func = function_named(off, 'ce_length')
    il = MediumLevelILFunction(func.arch, low_level_il=func.llil)
    il.prepare_to_copy_function(func.mlil)
    il.prepare_to_copy_block(func.mlil.basic_blocks[0])
    il.set_current_address(func.start, func.arch)
    slot = Variable(func, VariableSourceType.StackVariableSourceType, 0, -32)
    write = il.set_var(4, slot, il.const(4, 0x434241))
    il.set_expr_attributes(write, {attribute}); il.append(write)
    il.append(il.call([], il.const_pointer(func.arch.address_size, func.start), [il.address_of(slot)]))
    il.append(il.ret([])); il.finalize(); il.generate_ssa_form()
    new, report = constant_buffers.run(il)
    assert new is il and not report.regions and not report.strings
    assert any('protected' in note for note in report.notes)
