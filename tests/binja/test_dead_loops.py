from dataclasses import replace
from types import SimpleNamespace
import pytest
import z3
from binaryninja import HighLevelILFunction,HighLevelILOperation as H,ILInstructionAttribute as Attr,Settings
from arch import IDS,MATRIX,sample
from helpers import open_sample,set_types,function_named,settle
from flowcases import text
from flag_model import execute
from binja_d810.binja import dead_loops,settings,log
from binja_d810.binja import control_cleanup
from binja_d810.binja.control_cleanup import walk
from binja_d810.deadline import Deadline

TYPES={n:f'uint32_t {n}(uint32_t x,uint32_t y,uint32_t z)' for n in ('dl_dead','dl_live','dl_store')}


@pytest.fixture(scope='module',params=MATRIX,ids=IDS)
def builds(request):
    # The earlier MLIL junk-loop pass can already remove some compiler layouts. Isolate the
    # new HLIL pass, which also runs after branch equivalence has made loop outputs dead.
    on=open_sample(sample('control_cleanup',*request.param),d810=True,options={settings.UNFLATTEN:False})
    off=open_sample(sample('control_cleanup',*request.param),d810=True,
                    options={settings.DEAD_FINITE_LOOPS:False,settings.UNFLATTEN:False})
    for bv in (on,off):set_types(bv,TYPES);settle(bv)
    yield on,off
    on.file.close();off.file.close()


def test_dead_c_loop_is_removed_with_exact_result(builds):
    f=function_named(builds[0],'dl_dead')
    assert any(r.control_loops for r in log.latest_reports(f).values()),{k:r.control for k,r in log.latest_reports(f).items()}
    assert not any(n.operation in dead_loops._LOOPS for n in walk(f.hlil.root,lambda:None)),text(f)
    z=z3.BitVec('z',32)
    for path,result,writes in execute(f):
        solver=z3.Solver();solver.add(path,result!=z+3)
        assert solver.check()==z3.unsat
        assert not writes


@pytest.mark.parametrize('name',('dl_live','dl_store'))
def test_live_data_and_memory_effects_are_unchanged(builds,name):
    on,off=builds
    f=function_named(on,name)
    assert not any(r.control_loops for r in log.latest_reports(f).values())
    assert text(f)==text(function_named(off,name))


def program(func,mode):
    il=HighLevelILFunction(func.arch,source_func=func)
    x,y,z=func.parameter_vars
    read=lambda v:il.var(4,v)
    init=il.assign(4,read(x),il.const(4,0))
    value=il.mult(4,il.xor_expr(4,read(y),read(z)),il.const(4,7))
    if mode=='load':value=il.deref(4,il.const_pointer(func.arch.address_size,func.start),None)
    assignment=il.assign(4,read(y),value)
    if mode=='protected':il.set_expr_attributes(assignment,{Attr.ILPreventDeadStoreElimination})
    if mode=='store':assignment=il.assign(4,il.deref(4,il.const_pointer(func.arch.address_size,func.start),None),value)
    if mode=='partial':assignment=il.assign(1,il.var(1,y),il.const(1,4))
    control=read(y if mode=='unknown' else x)
    guard=il.compare_equal(4,control,il.const(4,7))
    update=il.assign(4,read(x),il.add(4,read(x),il.const(4,0 if mode=='infinite' else 1)))
    body=il.block([assignment,il.if_expr(guard,il.break_expr(),il.nop()),update])
    if mode=='continue':body=il.block([il.continue_expr(),update])
    loop=il.while_expr(il.const(0,1),body)
    if mode=='for':
        loop=il.for_expr(init,il.compare_not_equal(4,read(x),il.const(4,8)),update,il.block([assignment]))
        init=il.nop()
    if mode=='post':loop=il.do_while_expr(body,il.const(0,1))
    result=read(y) if mode=='live' else read(z)
    prefix=[init]
    if mode=='embedded':result=il.add(4,il.assign(4,read(x),il.const(4,9)),read(z))
    if mode=='seed_call':prefix=[il.var_init(4,y,il.call(il.const_pointer(func.arch.address_size,func.start),[read(z)])),init]
    if mode=='aliased':
        # Standalone HLIL's aliased_vars may be empty even after generate_ssa_form([x]).
        # Make the address observably escape as well, instead of relying on that metadata.
        prefix.append(il.assign(func.arch.address_size,
                                il.deref(func.arch.address_size,il.const_pointer(func.arch.address_size,func.start),None),
                                il.address_of(read(x))))
    root=il.block(prefix+[loop,il.ret([result])])
    il.root=il.get_expr(root);il.finalize();il.generate_ssa_form([x] if mode=='aliased' else [])
    return il


@pytest.mark.parametrize('mode',('dead','for','post','embedded','seed_call','live','load','store','protected','partial','aliased','unknown','infinite','continue'))
def test_native_effect_deadness_and_termination_boundaries(builds,mode):
    f=function_named(builds[1],'dl_dead');il=program(f,mode);before=str(il.root)
    report=dead_loops.run(il,dead_loops.Limits(),Deadline.after(5))
    if mode in ('dead','for','post','embedded','seed_call'):
        assert report.loops==1 and report.iterations==8,report.notes
        assert not any(n.operation in dead_loops._LOOPS for n in walk(il.root,lambda:None))
        if mode=='seed_call':
            assert sum(n.operation==H.HLIL_CALL for n in walk(il.root,lambda:None))==1
            return
        if mode!='embedded':
            assert not any(n.operation in (H.HLIL_ASSIGN,H.HLIL_VAR_INIT,H.HLIL_VAR_DECLARE)
                           for n in walk(il.root,lambda:None)),str(il.root)
        for path,result,writes in execute(SimpleNamespace(hlil=il,parameter_vars=f.parameter_vars)):
            expected=z3.BitVec('z',32)+(9 if mode=='embedded' else 0)
            solver=z3.Solver();solver.add(path,result!=expected)
            assert solver.check()==z3.unsat
            assert not writes
    else:
        assert report.loops==0,report.notes
        assert str(il.root)==before


def test_caps_and_failure_restore_native_il(builds,monkeypatch):
    f=function_named(builds[1],'dl_dead')
    for limits in (replace(dead_loops.Limits(),iterations=7),replace(dead_loops.Limits(),work=1)):
        il=program(f,'dead');before=str(il.root)
        report=dead_loops.run(il,limits,Deadline.after(5))
        assert report.loops==0 and str(il.root)==before
    original=dead_loops.HLILAdapter.finish
    for fail_at in (1,2):
        il=program(f,'dead');before=str(il.root);calls=0
        def fail_once(adapter):
            nonlocal calls
            calls+=1
            if calls==fail_at:raise ValueError('injected finalize failure')
            original(adapter)
        monkeypatch.setattr(dead_loops.HLILAdapter,'finish',fail_once)
        report=dead_loops.run(il,dead_loops.Limits(),Deadline.after(5))
        assert report.loops==0 and 'dead-loop changes rolled back' in report.notes
        assert str(il.root)==before


def test_straight_line_code_skips_expression_traversal(builds):
    f=function_named(builds[1],'dl_dead')
    il=HighLevelILFunction(f.arch,source_func=f)
    value=il.var(4,f.parameter_vars[0])
    for _ in range(256):value=il.add(4,value,il.const(4,1))
    il.root=il.get_expr(il.block([il.ret([value])]))
    il.finalize();il.generate_ssa_form([])
    for module in (control_cleanup,dead_loops):
        report=module.run(il,replace(module.Limits(),work=8),Deadline.after(5))
        assert not report.exhausted,report.notes


def test_disabled_feature_restores_the_loop():
    assert Settings().get_bool(settings.DEAD_FINITE_LOOPS)
    bv=open_sample('control_cleanup_O0.dylib',d810=True,
                   options={settings.DEAD_FINITE_LOOPS:False,settings.UNFLATTEN:False})
    try:
        set_types(bv,TYPES);settle(bv)
        f=function_named(bv,'dl_dead');before=text(f)
        Settings().set_bool(settings.DEAD_FINITE_LOOPS,True,bv);f.reanalyze();settle(bv)
        assert any(r.control_loops for r in log.latest_reports(f).values())
        Settings().set_bool(settings.DEAD_FINITE_LOOPS,False,bv);f.reanalyze();settle(bv)
        assert text(f)==before
    finally:bv.file.close()
