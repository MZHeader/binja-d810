"""Structured branch proofs with independent final-value and observable-effect checks."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
import z3
from binaryninja import HighLevelILFunction,HighLevelILOperation as H,ILInstructionAttribute as Attr,Settings
from arch import IDS,MATRIX,sample
from helpers import open_sample,set_types,function_named,settle
from flowcases import text
from flag_model import execute
from binja_d810.binja import control_cleanup as cleanup,settings,log
from binja_d810.deadline import Deadline

NAMES=tuple(f'ce_equal_{w}' for w in (8,16,32,64))+('ce_different','ce_store','ce_guard_load')
TYPES={n:f'uint32_t {n}(uint32_t x,uint32_t y,uint32_t z)' for n in NAMES}


@pytest.fixture(scope='module',params=MATRIX,ids=IDS)
def builds(request):
    on=open_sample(sample('control_cleanup',*request.param),d810=True)
    off=open_sample(sample('control_cleanup',*request.param),d810=True,options={settings.EQUIVALENT_BRANCHES:False})
    for bv in (on,off):set_types(bv,TYPES);settle(bv)
    yield on,off
    on.file.close();off.file.close()


@pytest.mark.parametrize('width',(8,16,32,64))
def test_c_branch_results_for_all_inputs(builds,width):
    x,y=z3.BitVecs('x y',32)
    expected=(x^(x+y)) & ((1<<min(width,32))-1)
    for bv in builds:
        f=function_named(bv,f'ce_equal_{width}')
        rows=execute(f)
        solver=z3.Solver();solver.set(timeout=2000)
        solver.add(z3.Or([z3.And(path,result!=expected) for path,result,_ in rows]))
        assert solver.check()==z3.unsat,text(f)
        assert all(not writes for _,_,writes in rows)


def test_live_differences_and_stores_survive(builds):
    x,y,z=z3.BitVecs('x y z',32)
    for bv in builds:
        for name in ('ce_different','ce_store'):
            expected=z3.If(z!=0,x+1,y+1) if name=='ce_different' else x+y
            for path,result,writes in execute(function_named(bv,name)):
                solver=z3.Solver();solver.add(path,result!=expected)
                assert solver.check()==z3.unsat
                if name=='ce_store':
                    assert len(writes)==1
                    solver=z3.Solver();solver.add(path,writes[0][1]!=z3.If(z!=0,x,y))
                    assert solver.check()==z3.unsat


def program(func,mode='equal'):
    il=HighLevelILFunction(func.arch,source_func=func)
    x,y,z=func.parameter_vars
    read=lambda v:il.var(4,v)
    a=il.add(4,il.xor_expr(4,read(x),read(y)),il.mult(4,il.const(4,2),il.and_expr(4,read(x),read(y))))
    b=il.add(4,read(x),read(y))
    if mode=='different':b=il.add(4,b,il.const(4,1))
    yes=il.assign(4,read(x),a);no=il.assign(4,read(x),b)
    if mode=='protected':il.set_expr_attributes(yes,{Attr.ILPreventDeadStoreElimination})
    guard=il.compare_not_equal(4,read(z),il.const(4,0))
    if mode=='load':guard=il.compare_not_equal(4,il.deref(4,il.const_pointer(func.arch.address_size,func.start),None),il.const(4,0))
    root=il.block([il.if_expr(guard,il.block([yes]),il.block([no])),il.ret([read(x)])])
    il.root=il.get_expr(root);il.finalize();il.generate_ssa_form([])
    return il


@pytest.mark.parametrize('mode',('equal','different','protected','load'))
def test_native_hlil_proof_and_effect_boundaries(builds,mode):
    f=function_named(builds[1],'ce_equal_32');il=program(f,mode)
    before=str(il.root)
    report=cleanup.run(il,cleanup.Limits(),Deadline.after(5))
    if mode!='equal':
        assert report.branches==0,report.notes
        assert str(il.root)==before
    else:
        assert report.branches==1,report.notes
        assert 'if (' not in str(il.root)
        x,y=z3.BitVecs('x y',32)
        for path,result,writes in execute(SimpleNamespace(hlil=il,parameter_vars=f.parameter_vars)):
            solver=z3.Solver();solver.add(path,result!=x+y)
            assert solver.check()==z3.unsat
            assert not writes


def test_work_limit_leaves_native_il_unchanged(builds):
    il=program(function_named(builds[1],'ce_equal_32'));before=str(il.root)
    report=cleanup.run(il,replace(cleanup.Limits(),work=1),Deadline.after(5))
    assert report.branches==0 and report.exhausted
    assert str(il.root)==before


def test_failure_rolls_back_native_edits(builds,monkeypatch):
    il=program(function_named(builds[1],'ce_equal_32'));before=str(il.root)
    original=cleanup.HLILAdapter.finish;calls=0
    def fail_once(adapter):
        nonlocal calls
        calls+=1
        if calls==1:raise ValueError('injected finalize failure')
        original(adapter)
    monkeypatch.setattr(cleanup.HLILAdapter,'finish',fail_once)
    report=cleanup.run(il,cleanup.Limits(),Deadline.after(5))
    assert report.branches==0 and 'control changes rolled back' in report.notes
    assert str(il.root)==before


def test_c_fixture_uses_the_workflow_and_disable_restores():
    assert Settings().get_bool(settings.EQUIVALENT_BRANCHES)
    bv=open_sample('control_cleanup_O0.dylib',d810=True,options={settings.EQUIVALENT_BRANCHES:False})
    try:
        set_types(bv,TYPES);settle(bv)
        before={n:text(function_named(bv,n)) for n in NAMES}
        Settings().set_bool(settings.EQUIVALENT_BRANCHES,True,bv)
        for n in NAMES:function_named(bv,n).reanalyze()
        settle(bv)
        changed=[n for n in NAMES if any(r.control_branches for r in log.latest_reports(function_named(bv,n)).values())]
        assert changed,{n:log.latest_reports(function_named(bv,n)) for n in NAMES}
        Settings().set_bool(settings.EQUIVALENT_BRANCHES,False,bv)
        for n in NAMES:function_named(bv,n).reanalyze()
        settle(bv)
        assert {n:text(function_named(bv,n)) for n in NAMES}==before
    finally:bv.file.close()
