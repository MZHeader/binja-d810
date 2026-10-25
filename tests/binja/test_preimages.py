import json
from pathlib import Path
from dataclasses import replace
import pytest
from binaryninja import HighLevelILFunction,MediumLevelILFunction,HighLevelILOperation as H,ILInstructionAttribute as Attr,Settings
from arch import IDS,MATRIX,sample
from helpers import open_sample,set_types,function_named,settle
from flowcases import text
from oracle import function_result,reference,compare
from binja_d810.binja import comparison_preimages as preimages,settings,log
from binja_d810.deadline import Deadline

CASES=json.loads((Path(__file__).resolve().parents[1]/'samples/build/preimages.json').read_text())
TYPES={c['name']:f"uint32_t {c['name']}(uint{c['width']}_t x)" for c in CASES}


@pytest.fixture(scope='module',params=MATRIX,ids=IDS)
def builds(request):
    on=open_sample(sample('preimages',*request.param),d810=True)
    off=open_sample(sample('preimages',*request.param),d810=True,options={settings.COMPARISON_PREIMAGES:False})
    for bv in (on,off):set_types(bv,TYPES);settle(bv)
    yield on,off
    on.file.close();off.file.close()


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['name'])
def test_encoded_comparisons_for_every_input(builds,case):
    expected=reference(f"x == {case['value']}",case['width'])
    if case['width']<32:expected=('op','zx',32,expected)
    elif case['width']>32:expected=('bits',expected,0,32)
    for bv in builds:
        f=function_named(bv,case['name'])
        result=compare(function_result(f),expected,32)
        assert result.kind in ('proven','exhaustive'),(result,text(f))


def test_the_feature_changes_native_output(builds):
    on,off=builds
    changed=[c['name'] for c in CASES if any(r.preimage_conditions for r in log.latest_reports(function_named(on,c['name'])).values())]
    assert changed
    assert any(text(function_named(on,n))!=text(function_named(off,n)) for n in changed)
    assert not any(r.errors for c in CASES for r in log.latest_reports(function_named(on,c['name'])).values())


def program(f,mode):
    il=HighLevelILFunction(f.arch,source_func=f);x=f.parameter_vars[0]
    value=il.var(4,x)
    if mode=='load':value=il.deref(4,il.const_pointer(f.arch.address_size,f.start),None)
    value=il.add(4,il.mult(4,value,il.const(4,7)),il.const(4,23))
    if mode=='protected':il.set_expr_attributes(value,{Attr.ILPreventDeadStoreElimination})
    condition=(il.compare_unsigned_less_than if mode=='ordered' else il.compare_equal)(4,value,il.const(4,72))
    ret=il.ret([il.bool_to_int(4,condition)])
    if mode=='protected_container':il.set_expr_attributes(ret,{Attr.ILPreventDeadStoreElimination})
    il.root=il.get_expr(il.block([ret]))
    il.finalize();il.generate_ssa_form([])
    return il


@pytest.mark.parametrize('mode',('positive','protected','protected_container','load','ordered'))
def test_native_purity_and_ordering_boundaries(builds,mode):
    il=program(function_named(builds[1],'pi_32_0'),mode);before=str(il.root)
    report=preimages.run(il,'hlil',preimages.Limits(),Deadline.after(5))
    if mode=='positive':assert report.changed==1,report.notes
    else:
        assert report.changed==0,report.notes
        assert str(il.root)==before


def test_work_bound_and_rollback(builds,monkeypatch):
    f=function_named(builds[1],'pi_32_0');il=program(f,'positive');before=str(il.root)
    report=preimages.run(il,'hlil',replace(preimages.Limits(),work=1),Deadline.after(5))
    assert report.changed==0 and report.exhausted and str(il.root)==before
    original=preimages.HLILAdapter.finish;calls=0
    def fail_once(adapter):
        nonlocal calls
        calls+=1
        if calls==1:raise ValueError('injected failure')
        original(adapter)
    monkeypatch.setattr(preimages.HLILAdapter,'finish',fail_once)
    report=preimages.run(il,'hlil',preimages.Limits(),Deadline.after(5))
    assert report.changed==0 and str(il.root)==before
    assert 'comparison rewrites rolled back' in report.notes


@pytest.mark.parametrize('protected',(False,True))
def test_mlil_protected_ancestor(builds,protected):
    f=function_named(builds[1],'pi_32_0');il=MediumLevelILFunction(f.arch,low_level_il=f.llil)
    il.prepare_to_copy_function(f.mlil);il.prepare_to_copy_block(f.mlil.basic_blocks[0])
    il.set_current_address(f.start,f.arch)
    value=il.add(4,il.mult(4,il.var(4,f.parameter_vars[0]),il.const(4,7)),il.const(4,23))
    comparison=il.compare_equal(4,value,il.const(4,72))
    container=il.and_expr(4,il.bool_to_int(4,comparison),il.const(4,1))
    if protected:il.set_expr_attributes(container,{Attr.ILPreventDeadStoreElimination})
    il.append(il.ret([container]));il.finalize();il.generate_ssa_form([])
    before='\n'.join(str(n) for n in il.instructions)
    report=preimages.run(il,'mlil',preimages.Limits(),Deadline.after(5))
    assert report.changed==(0 if protected else 1),report.notes
    if protected:assert '\n'.join(str(n) for n in il.instructions)==before


def test_disable_restores_native_output():
    assert Settings().get_bool(settings.COMPARISON_PREIMAGES)
    bv=open_sample('preimages_O0.dylib',d810=True,options={settings.COMPARISON_PREIMAGES:False})
    try:
        set_types(bv,TYPES);settle(bv)
        before={c['name']:text(function_named(bv,c['name'])) for c in CASES}
        Settings().set_bool(settings.COMPARISON_PREIMAGES,True,bv)
        for c in CASES:function_named(bv,c['name']).reanalyze()
        settle(bv)
        assert any(r.preimage_conditions for c in CASES for r in log.latest_reports(function_named(bv,c['name'])).values())
        Settings().set_bool(settings.COMPARISON_PREIMAGES,False,bv)
        for c in CASES:function_named(bv,c['name']).reanalyze()
        settle(bv)
        assert {c['name']:text(function_named(bv,c['name'])) for c in CASES}==before
    finally:bv.file.close()
