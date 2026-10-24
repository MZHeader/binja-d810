"""All-path scratch-helper proofs and independent final-value checks on three architectures."""
import pytest
import z3
from binaryninja import ILInstructionAttribute as Attr, LowLevelILOperation as L, Settings
from arch import IDS, MATRIX, sample
from helpers import function_named, open_sample, set_types, settle
from flowcases import text
from flag_model import execute
from binja_d810.binja import log, settings, workflow
from binja_d810.binja.callees.lift import Limits, summarize
from binja_d810.binja.callees.rewrite import plan
from binja_d810.deadline import Deadline

GOOD = ('cfg_after', 'cfg_before', 'cfg_returns', 'cfg_arms', 'cfg_diamonds')
BAD = ('cfg_one_live', 'cfg_partial', 'cfg_arms_live', 'cfg_condition_read',
       'cfg_loop', 'cfg_escape', 'cfg_unbalanced', 'cfg_returnchanged')
TYPES = {name: f'uint32_t {name}(uint32_t x)' for name in GOOD + BAD}
TYPES['cfg_escape'] = 'void* cfg_escape(uint32_t x)'


@pytest.fixture(scope='module', params=MATRIX, ids=IDS)
def builds(request):
    off = open_sample(sample('callee_cfg', *request.param), d810=False)
    on = open_sample(sample('callee_cfg', *request.param), d810=True)
    for bv in (off,on): set_types(bv,TYPES); settle(bv)
    yield request.param,on,off
    on.file.close(); off.file.close()


def calls(func):
    return {i.instr_index for i in func.lifted_il.instructions if i.operation == L.LLIL_CALL}


def model(name):
    def arithmetic(x):
        rotated = ((x >> 4) | (x << 28)) & 0xffffffff
        return (rotated * 13 + 23) & 0xffffffff
    a,b = arithmetic(37),arithmetic(53)
    x = z3.BitVec('x',32)
    if name in ('cfg_after','cfg_diamonds'): return a + z3.If(x != 0,z3.BitVecVal(1,32),z3.BitVecVal(0,32))
    if name == 'cfg_returns': return z3.If(x != 0,z3.BitVecVal(a^1,32),z3.BitVecVal(a,32))
    return z3.If(x != 0,z3.BitVecVal(b,32),z3.BitVecVal(a,32))


@pytest.mark.parametrize('name',GOOD)
def test_branches_and_multiple_returns_remove_only_proven_helper_calls(builds,name):
    _,on,off = builds
    original = function_named(off,name)
    sites,notes = plan(original.lifted_il,original,Limits(),Deadline.after(3))
    assert calls(original) and set(sites) == calls(original), notes
    current = function_named(on,name)
    assert 'cfg_leaf(' not in text(current), text(current)
    assert log.latest_report(current,'mlil').callee_calls == len(sites)
    assert any('blocks checked once' in note for note in log.latest_report(current,'mlil').callees)
    solver = z3.Solver(); solver.set(timeout=2000)
    returns = execute(current)
    assert all(not writes for _,_,writes in returns)
    solver.add(z3.Or([z3.And(path,result != model(name)) for path,result,_ in returns]))
    assert solver.check() == z3.unsat, text(current)
    # Check the oracle did not vacuously discard any concrete input path.
    solver = z3.Solver(); solver.add(z3.Not(z3.Or([path for path,_,_ in returns])))
    assert solver.check() == z3.unsat


@pytest.mark.parametrize('name',BAD)
def test_any_live_or_escaping_path_and_unknown_frame_exit_keep_calls(builds,name):
    _,on,off = builds
    original = function_named(off,name)
    sites,notes = plan(original.lifted_il,original,Limits(),Deadline.after(3))
    assert calls(original) and not sites and notes, (name,notes)
    assert not log.latest_report(function_named(on,name),'mlil').callee_calls


def test_frame_limits_and_deadline_reject_the_whole_unfinished_proof(builds):
    _,_,off = builds
    func = function_named(off,'cfg_after')
    for limits,deadline in ((Limits(frame_work=1),Deadline.after(3)),
                            (Limits(frame_cfg=False),Deadline.after(3)),
                            (Limits(),Deadline.after(0))):
        sites,notes = plan(func.lifted_il,func,limits,deadline)
        assert not sites and notes


def test_frame_settings_defaults():
    s = Settings()
    assert s.get_bool(settings.CALLEE_FRAME_CFG)
    assert s.get_integer(settings.CALLEE_FRAME_WORK) == 40000


def test_a_return_opcode_does_not_prove_the_saved_return_target(builds):
    _,_,off = builds
    result = summarize(function_named(off,'cfg_bad_return_leaf'))
    assert result.summary is None and 'return target' in result.reason


def test_protected_branch_does_not_authorize_substitution(builds):
    _,_,off = builds
    func = function_named(off,'cfg_after'); il = func.lifted_il
    branch = next(i for i in il.instructions if i.operation == L.LLIL_IF)
    original = branch.attributes
    try:
        il.set_expr_attributes(branch.expr_index,{Attr.ILPreventDeadStoreElimination})
        sites,notes = plan(il,func,Limits(),Deadline.after(3))
        assert not sites and any('protected caller branch' in note for note in notes)
    finally: il.set_expr_attributes(branch.expr_index,original)


@pytest.mark.parametrize('build',MATRIX,ids=IDS)
def test_disabling_cfg_proof_and_master_setting_restore_calls(build):
    bv = open_sample(sample('callee_cfg',*build),d810=True)
    try:
        set_types(bv,TYPES); settle(bv)
        func = function_named(bv,'cfg_after')
        assert log.latest_report(func,'mlil').callee_calls
        Settings().set_bool(settings.CALLEE_FRAME_CFG,False,bv)
        func.reanalyze(); settle(bv)
        assert not log.latest_report(func,'mlil').callee_calls
        assert 'cfg_leaf(' in text(func)
        Settings().set_bool(settings.CALLEE_FRAME_CFG,True,bv)
        func.reanalyze(); settle(bv)
        assert log.latest_report(func,'mlil').callee_calls
        Settings().set_bool(workflow.ACTIVITY_SETTING,False,bv)
        func.reanalyze(); settle(bv)
        assert 'cfg_leaf(' in text(func)
    finally: bv.file.close()
