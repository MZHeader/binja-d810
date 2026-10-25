import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import pytest,z3
from binaryninja import HighLevelILFunction,HighLevelILOperation as H,ILInstructionAttribute as Attr,Settings
from arch import IDS,MATRIX,sample
from helpers import open_sample,set_types,function_named,settle
from flowcases import text
from flag_model import execute
from binja_d810.binja import recurrence_preimages as recurrence,settings,log,dead_loops
from binja_d810.deadline import Deadline

CASES=json.loads((Path(__file__).resolve().parents[1]/'samples/build/recurrences.json').read_text())
TYPES={c['name']:f"uint32_t {c['name']}(uint32_t x,uint32_t y,uint32_t z)" for c in CASES}


@pytest.fixture(scope='module',params=MATRIX,ids=IDS)
def builds(request):
    on=open_sample(sample('recurrences',*request.param),d810=True)
    off=open_sample(sample('recurrences',*request.param),d810=True,options={settings.RECURRENCE_PREIMAGES:False})
    for bv in (on,off):set_types(bv,TYPES);settle(bv)
    yield on,off
    on.file.close();off.file.close()


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['name'])
def test_final_predicates_match_exhaustive_python_models(builds,case):
    x=z3.BitVec('x',32)
    expected=z3.If(z3.Or([(x&255)==v for v in case['matches']]),z3.BitVecVal(1,32),z3.BitVecVal(0,32))
    for bv in builds:
        f=function_named(bv,case['name']);rows=execute(f,loop_limit=8,local_memory=True)
        solver=z3.Solver();solver.set(timeout=5000)
        solver.add(z3.Or([z3.And(path,value!=expected) for path,value,_ in rows]))
        assert solver.check()==z3.unsat,text(f)
        assert all(not writes for _,_,writes in rows)


def test_the_workflow_recovers_recurrence_predicates(builds):
    on,off=builds
    changed=[c['name'] for c in CASES if any(r.recurrence_conditions for r in log.latest_reports(function_named(on,c['name'])).values())]
    assert changed,{c['name']:[r.preimages for r in log.latest_reports(function_named(on,c['name'])).values()] for c in CASES}
    assert any(text(function_named(on,n))!=text(function_named(off,n)) for n in changed)
    assert any(note.startswith('reserved ') for n in changed
               for r in log.latest_reports(function_named(on,n)).values() for note in r.preimages)
    assert not any(note.startswith('reserved ') for c in CASES
                   for r in log.latest_reports(function_named(off,c['name'])).values() for note in r.preimages)


def target(x):
    state=0x1973
    for i in range(4):state=((state^((x>>(2*i))&3))*257+17)&0xffffffff
    return state


def program(f,mode):
    il=HighLevelILFunction(f.arch,source_func=f);x,i,s=f.parameter_vars
    read=lambda v:il.var(4,v)
    init=[il.assign(4,read(i),il.const(4,0)),il.assign(4,read(s),il.const(4,0x1973))]
    counter=il.struct_field(1,read(i),0,0xffffffffffffffff) if mode=='field' else read(i)
    if mode=='field':counter=il.zero_extend(4,counter)
    digit=il.and_expr(4,il.logical_shift_right(4,read(x),il.mult(4,counter,il.const(4,2))),il.const(4,3))
    value=il.add(4,il.mult(4,il.xor_expr(4,read(s),digit),il.const(4,257)),il.const(4,17))
    if mode=='load':value=il.deref(4,il.const_pointer(f.arch.address_size,f.start),None)
    assignment=il.assign(4,read(s),value)
    if mode=='protected':il.set_expr_attributes(assignment,{Attr.ILPreventDeadStoreElimination})
    update=il.assign(4,read(i),il.add(4,read(i),il.const(4,1)))
    loop=il.while_expr(il.compare_unsigned_less_than(4,read(i),read(x) if mode=='unknown' else il.const(4,4)),
                       il.block([assignment,update]))
    body=init+[loop]
    if mode=='stale':body.append(il.assign(4,read(x),il.add(4,read(x),il.const(4,1))))
    if mode=='stale_field':body.append(il.assign(1,il.struct_field(1,read(x),0,0xffffffffffffffff),il.const(1,0)))
    predicate=il.compare_equal(4,read(s),il.const(4,target(109)))
    result=il.bool_to_int(4,predicate)
    if mode=='boolean_arithmetic':result=il.and_expr(4,predicate,il.const(4,1))
    if mode=='live':result=il.add(4,result,read(s))
    body.append(il.ret([result]));root=il.block(body)
    if mode=='protected_container':il.set_expr_attributes(root,{Attr.ILPreventDeadStoreElimination})
    il.root=il.get_expr(root);il.finalize();il.generate_ssa_form([])
    return il


@pytest.mark.parametrize('mode',('positive','live','stale','stale_field','unknown','load','protected','protected_container','field','boolean_arithmetic'))
def test_native_scope_and_effect_boundaries(builds,mode):
    f=function_named(builds[1],'rp_32_0');il=program(f,mode);before=str(il.root)
    report=recurrence.run(il,recurrence.Limits(),Deadline.after(5))
    if mode in ('positive','live','field','boolean_arithmetic'):
        assert report.changed==1,report.notes
        if mode!='live':
            cleanup=dead_loops.run(il,dead_loops.Limits(),Deadline.after(5))
            assert cleanup.loops==(0 if mode=='field' else 1),cleanup.notes
            for path,result,writes in execute(SimpleNamespace(hlil=il,parameter_vars=f.parameter_vars),loop_limit=8):
                expected=z3.If((z3.BitVec('x',32)&255)==109,z3.BitVecVal(1,32),z3.BitVecVal(0,32))
                solver=z3.Solver();solver.add(path,result!=expected);assert solver.check()==z3.unsat
                assert not writes
        else:
            assert dead_loops.run(il,dead_loops.Limits(),Deadline.after(5)).loops==0
    else:
        assert report.changed==0,report.notes
        assert str(il.root)==before


def test_unproved_endian_field_layout_is_rejected(builds):
    il=program(function_named(builds[1],'rp_32_0'),'field')
    limits=recurrence.Limits();model=recurrence.Model(il,limits,recurrence.Work(limits,Deadline.after(5)))
    model.little=False
    fields=[n for n in recurrence.tree_nodes(il.root,lambda:None) if n.operation==H.HLIL_STRUCT_FIELD]
    assert fields
    with pytest.raises(ValueError,match='big-endian'):model.expression(fields[0])


def test_structural_reservation_preflight_is_bounded(builds):
    f=function_named(builds[1],'rp_32_0');il=program(f,'positive')
    assert recurrence.has_loops(il,recurrence.Limits(),Deadline.after(1))
    assert not recurrence.has_loops(il,replace(recurrence.Limits(),work=1),Deadline.after(1))
    assert not recurrence.has_loops(il,replace(recurrence.Limits(),instructions=1),Deadline.after(1))
    assert not recurrence.has_loops(il,recurrence.Limits(),Deadline.after(0))
    empty=HighLevelILFunction(f.arch,source_func=f)
    empty.root=empty.get_expr(empty.block([empty.ret([empty.const(4,0)])]));empty.finalize()
    assert not recurrence.has_loops(empty,recurrence.Limits(),Deadline.after(1))


def test_bounds_and_rollback(builds,monkeypatch):
    f=function_named(builds[1],'rp_32_0')
    for limits in (replace(recurrence.Limits(),work=1),replace(recurrence.Limits(),states=1),replace(recurrence.Limits(),iterations=3)):
        il=program(f,'positive');before=str(il.root)
        report=recurrence.run(il,limits,Deadline.after(5))
        assert report.changed==0 and str(il.root)==before
    il=program(f,'positive');before=str(il.root);original=recurrence.HLILAdapter.finish;calls=0
    def fail_once(adapter):
        nonlocal calls
        calls+=1
        if calls==1:raise ValueError('injected failure')
        original(adapter)
    monkeypatch.setattr(recurrence.HLILAdapter,'finish',fail_once)
    report=recurrence.run(il,recurrence.Limits(),Deadline.after(5))
    assert report.changed==0 and str(il.root)==before
    assert 'recurrence rewrites rolled back' in report.notes


def test_disabling_restores_the_original_loop():
    assert Settings().get_bool(settings.RECURRENCE_PREIMAGES)
    bv=open_sample('recurrences_O0.dylib',d810=True,options={settings.RECURRENCE_PREIMAGES:False})
    try:
        set_types(bv,TYPES);settle(bv);f=function_named(bv,'rp_32_0');before=text(f)
        Settings().set_bool(settings.RECURRENCE_PREIMAGES,True,bv);f.reanalyze();settle(bv)
        assert any(r.recurrence_conditions for r in log.latest_reports(f).values())
        Settings().set_bool(settings.RECURRENCE_PREIMAGES,False,bv);f.reanalyze();settle(bv)
        assert text(f)==before
    finally:bv.file.close()
