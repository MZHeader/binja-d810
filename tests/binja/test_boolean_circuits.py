"""Boolean encodings at four widths; final values checked by an independent Z3 interpreter."""
from dataclasses import replace
import pytest
import z3
from binaryninja import Settings,MediumLevelILFunction,MediumLevelILLabel,MediumLevelILOperation as M
from arch import IDS,MATRIX,sample
from helpers import open_sample,function_named,set_types,settle
from flowcases import text
from flag_model import execute
from binja_d810.binja import settings,log

NAMES=tuple(f'bc_{op}_{width}' for op in ('and','or','xor','select') for width in (8,16,32,64))
TYPES={n:f'uint32_t {n}(uint32_t x,uint32_t y,uint32_t z)' for n in NAMES+('bc_shared','bc_dead_merge','bc_live_merge')}

@pytest.fixture(scope='module',params=MATRIX,ids=IDS)
def builds(request):
    on=open_sample(sample('boolean_conditions',*request.param),d810=True)
    off=open_sample(sample('boolean_conditions',*request.param),d810=True,options={settings.FLAG_BOOLEAN:False})
    for bv in (on,off):set_types(bv,TYPES);settle(bv)
    yield request.param,on,off
    on.file.close();off.file.close()


def expected(name):
    x,y,z=(z3.BitVec(n,32) for n in ('x','y','z'))
    p,q,r=z3.ULT(x,y),y!=z,x==z
    return {'and':z3.And(p,q),'or':z3.Or(p,q),'xor':z3.Xor(p,q),'select':z3.If(p,q,r)}[name.split('_')[1]]


@pytest.mark.parametrize('name',NAMES)
def test_final_encoded_boolean_matches_every_input(builds,name):
    _,on,off=builds
    func=function_named(on,name)
    rows=execute(func)
    solver=z3.Solver();solver.set(timeout=2000)
    solver.add(z3.Or([z3.And(path,value!=z3.If(expected(name),z3.BitVecVal(1,value.size()),z3.BitVecVal(0,value.size()))) for path,value,_ in rows]))
    assert solver.check()==z3.unsat,text(func)
    assert all(not writes for _,_,writes in rows)


def test_the_new_circuit_pass_changes_native_output(builds):
    _,on,off=builds
    found=[name for name in NAMES if any('Boolean circuit simplified' in note for report in log.latest_reports(function_named(on,name)).values() for note in report.flags)]
    assert found
    assert any(text(function_named(on,name))!=text(function_named(off,name)) for name in found)


def test_emitted_boolean_values_remain_readable_by_general_lifters(builds):
    from binja_d810.binja.mlil.lift import lift as mlift
    from binja_d810.binja.hlil.lift import lift as hlift
    _,on,_=builds
    func=function_named(on,'bc_and_32')
    for ret in func.mlil.instructions:
        if ret.operation==M.MLIL_RET:
            assert mlift(ret.src[0].ssa_form,root_instr=ret,check_versions=False,max_nodes=2000) is not None
    for ret in func.hlil.instructions:
        if ret.operation.name=='HLIL_RET':
            assert hlift(ret.src[0].ssa_form,aliased=frozenset(func.hlil.aliased_vars),check_versions=False,max_nodes=2000) is not None


def test_control_flow_graph_reads_compound_one_bit_conditions(builds):
    from binja_d810.binja.unflatten.graph import MLILGraph
    _,_,off=builds
    f=function_named(off,'bc_and_32')
    il=MediumLevelILFunction(f.arch,low_level_il=f.llil)
    il.prepare_to_copy_function(f.mlil)
    il.prepare_to_copy_block(f.mlil.basic_blocks[0])
    il.set_current_address(f.start,f.arch)
    x,y,z=[il.var(4,v) for v in f.parameter_vars]
    p=il.compare_unsigned_less_than(4,x,y);q=il.compare_equal(4,y,z)
    condition=il.and_expr(0,p,il.not_expr(0,q))
    yes,no=MediumLevelILLabel(),MediumLevelILLabel()
    il.append(il.if_expr(condition,yes,no))
    il.mark_label(yes);il.append(il.ret([il.const(4,1)]))
    il.mark_label(no);il.append(il.ret([il.const(4,0)]))
    il.finalize();il.generate_ssa_form()
    graph=MLILGraph(il,view=off,ranges=[])
    assert graph.terminator(graph.entry).cond is not None


def test_shared_boolean_data_keeps_the_observable_store(builds):
    _,on,_=builds
    x,y=(z3.BitVec(n,32) for n in ('x','y'))
    for path,_,writes in execute(function_named(on,'bc_shared')):
        assert len(writes)==1
        solver=z3.Solver();solver.add(path,writes[0][1]!=z3.If(z3.ULT(x,y),z3.BitVecVal(1,32),z3.BitVecVal(0,32)))
        assert solver.check()==z3.unsat


def test_defaults_and_disable_restore_the_baseline():
    assert Settings().get_bool(settings.FLAG_BOOLEAN)
    assert Settings().get_integer(settings.FLAG_BOOLEAN_ATOMS)==4
    assert Settings().get_bool(settings.FLAG_PHI_USES)
    bv=open_sample('boolean_conditions_O0.dylib',d810=True,options={settings.FLAG_BOOLEAN:False})
    try:
        set_types(bv,TYPES);settle(bv)
        func=function_named(bv,'bc_xor_32');before=text(func)
        Settings().set_bool(settings.FLAG_BOOLEAN,True,bv);func.reanalyze();settle(bv)
        assert any('Boolean circuit simplified' in n for r in log.latest_reports(func).values() for n in r.flags)
        Settings().set_bool(settings.FLAG_BOOLEAN,False,bv);func.reanalyze();settle(bv)
        assert text(func)==before
    finally:bv.file.close()


@pytest.mark.parametrize('name',('bc_dead_merge','bc_live_merge'))
def test_loop_phi_values_preserve_every_observable_result(builds,name):
    _,on,_=builds
    x,y=(z3.BitVec(n,32) for n in ('x','y'))
    one,zero=z3.BitVecVal(1,32),z3.BitVecVal(0,32)
    expected=z3.If(z3.And(z3.ULT(x,4),y!=x),one,zero)
    if name=='bc_live_merge':expected+=z3.If((x==3)==(y!=3),one,zero)
    rows=execute(function_named(on,name),loop_limit=8)
    solver=z3.Solver();solver.set(timeout=2000)
    solver.add(z3.Or([z3.And(path,result!=expected) for path,result,_ in rows]))
    assert solver.check()==z3.unsat,text(function_named(on,name))
    assert all(not writes for _,_,writes in rows)
