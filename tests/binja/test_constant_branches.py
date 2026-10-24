"""Keep the selected arm's observable effects and control transfers exactly."""
from dataclasses import replace
from types import SimpleNamespace
import pytest,z3
from binaryninja import HighLevelILFunction,ILInstructionAttribute as Attr,Settings
from helpers import open_sample,set_types,function_named,settle
from flowcases import text
from arch import BUILDS,sample
from flag_model import execute
from binja_d810.binja import control_cleanup as cleanup,settings,log
from binja_d810.deadline import Deadline


@pytest.fixture(scope='module')
def owner():
    bv=open_sample('control_cleanup_O0.dylib',d810=False)
    set_types(bv,{'ce_equal_32':'uint32_t ce_equal_32(uint32_t x,uint32_t y,uint32_t z)'})
    yield function_named(bv,'ce_equal_32')
    bv.file.close()


def program(f,mode,taken):
    il=HighLevelILFunction(f.arch,source_func=f);x,y,z=f.parameter_vars
    read=lambda v:il.var(4,v)
    guard=il.const(0,int(taken))
    if mode=='identity':guard=il.compare_equal(4,il.xor_expr(4,read(x),read(x)),il.const(4,0))
    if mode=='load':guard=il.compare_equal(4,il.deref(4,il.const_pointer(8,0x1000),None),il.const(4,0))
    if mode in ('ternary','protected_container'):
        branch=il.if_expr(guard,il.add(4,read(x),il.const(4,3)),il.sub(4,read(x),il.const(4,7)))
        root=il.ret([branch])
        if mode=='protected_container':il.set_expr_attributes(root,{Attr.ILPreventDeadStoreElimination})
    else:
        yes=il.block([il.assign(4,il.deref(4,il.const_pointer(8,0x1000),None),read(y)),il.ret([read(x)])])
        no=il.block([il.assign(4,il.deref(4,il.const_pointer(8,0x2000),None),read(z)),il.ret([read(y)])])
        if mode=='label':no=il.block([il.label(42),no])
        if mode=='protected':il.set_expr_attributes(no if taken else yes,{Attr.ILPreventDeadStoreElimination})
        root=il.if_expr(guard,yes,no)
    il.root=il.get_expr(il.block([root]));il.finalize();il.generate_ssa_form([])
    return il


@pytest.mark.parametrize('taken',(False,True))
@pytest.mark.parametrize('mode',('effects','identity','ternary','protected','protected_container','load','label'))
def test_constant_selection(owner,mode,taken):
    il=program(owner,mode,taken);before=str(il.root)
    report=cleanup.run(il,replace(cleanup.Limits(),equivalents=False),Deadline.after(5))
    if mode in ('protected','protected_container','load','label'):
        assert report.constants==0 and str(il.root)==before,report.notes
        return
    assert report.constants==1,report.notes
    x,y,z=z3.BitVecs('x y z',32)
    selected=taken or mode=='identity'
    for path,result,writes in execute(SimpleNamespace(hlil=il,parameter_vars=owner.parameter_vars)):
        expected=(x+3 if selected else x-7) if mode=='ternary' else (x if selected else y)
        solver=z3.Solver();solver.add(path,result!=expected);assert solver.check()==z3.unsat
        if mode=='ternary':assert not writes
        else:
            assert len(writes)==1
            address,datum=writes[0]
            solver=z3.Solver();solver.add(path,z3.Or(address!=(0x1000 if selected else 0x2000),datum!=(y if selected else z)))
            assert solver.check()==z3.unsat


def test_limits_switch_and_rollback(owner,monkeypatch):
    for limits in (replace(cleanup.Limits(),constants=False),replace(cleanup.Limits(),work=1)):
        il=program(owner,'effects',True);before=str(il.root)
        report=cleanup.run(il,limits,Deadline.after(5))
        assert report.constants==0 and str(il.root)==before
    il=program(owner,'effects',True);before=str(il.root);original=cleanup.HLILAdapter.finish;calls=0
    def fail_once(adapter):
        nonlocal calls
        calls+=1
        if calls==1:raise ValueError('injected failure')
        original(adapter)
    monkeypatch.setattr(cleanup.HLILAdapter,'finish',fail_once)
    report=cleanup.run(il,cleanup.Limits(),Deadline.after(5))
    assert report.constants==0 and str(il.root)==before
    assert 'control changes rolled back' in report.notes


@pytest.mark.parametrize('build',BUILDS)
def test_c_workflow_and_disable_restore(build):
    bv=open_sample(sample('control_cleanup',build,'O0'),d810=True,options={settings.CONSTANT_BRANCHES:False})
    try:
        set_types(bv,{'cb_identity':'uint32_t cb_identity(uint32_t x,uint32_t y,uint32_t z)'})
        settle(bv);f=function_named(bv,'cb_identity');before=text(f)
        Settings().set_bool(settings.CONSTANT_BRANCHES,True,bv);f.reanalyze();settle(bv)
        assert any(r.constant_branches for r in log.latest_reports(f).values()),[r.control for r in log.latest_reports(f).values()]
        for path,result,writes in execute(f):
            x,y=z3.BitVecs('x y',32)
            solver=z3.Solver();solver.add(path,result!=y+3);assert solver.check()==z3.unsat
            assert len(writes)==1
            solver=z3.Solver();solver.add(path,writes[0][1]!=x);assert solver.check()==z3.unsat
        Settings().set_bool(settings.CONSTANT_BRANCHES,False,bv);f.reanalyze();settle(bv)
        assert text(f)==before
    finally:bv.file.close()
