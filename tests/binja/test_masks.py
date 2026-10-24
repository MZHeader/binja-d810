"""Random mask constants on ARM64, x64 PE/ELF and i686, O0/O2; independent final-IL proof."""
import json
from pathlib import Path
import pytest
from binaryninja import Settings
from arch import IDS,MATRIX,sample
from helpers import open_sample,function_named,set_types,settle
from flowcases import text
from oracle import function_result,reference,compare
from binja_d810.binja import settings,log

CASES=json.loads((Path(__file__).resolve().parents[1]/'samples/build/masks.json').read_text())
TYPES={c['name']:f"uint{c['width']}_t {c['name']}(uint{c['width']}_t x,uint{c['width']}_t y)" for c in CASES}


@pytest.fixture(scope='module',params=MATRIX,ids=IDS)
def builds(request):
    on=open_sample(sample('masks',*request.param),d810=True)
    off=open_sample(sample('masks',*request.param),d810=True,options={settings.BITWISE_MASKS:False})
    for bv in (on,off):set_types(bv,TYPES);settle(bv)
    yield on,off
    on.file.close();off.file.close()


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['name'])
def test_final_value_for_every_input(builds,case):
    for bv in builds:
        result=compare(function_result(function_named(bv,case['name'])),reference(case['expected'],case['width']),case['width'])
        assert result.kind in ('proven','exhaustive'),result


def test_factoring_changes_native_output(builds):
    on,off=builds
    changed=[c['name'] for c in CASES if text(function_named(on,c['name']))!=text(function_named(off,c['name']))]
    assert changed
    assert any('bitwise' in str(log.latest_reports(function_named(on,n))) for n in changed)
    for case in CASES:
        for report in log.latest_reports(function_named(on,case['name'])).values():
            assert all(r.verdict=='PROVEN' for r in report.rewrites if 'bitwise' in r.source)


def test_defaults_and_disable_restore_output():
    assert Settings().get_bool(settings.BITWISE_MASKS)
    assert Settings().get_integer(settings.BITWISE_INPUTS)==4
    assert Settings().get_integer(settings.BITWISE_CONSTANTS)==4
    bv=open_sample('masks_O0.dylib',d810=True,options={settings.BITWISE_MASKS:False})
    try:
        set_types(bv,TYPES);settle(bv)
        f=function_named(bv,'mask_32_0');before=text(f)
        Settings().set_bool(settings.BITWISE_MASKS,True,bv);f.reanalyze();settle(bv)
        assert text(f)!=before
        Settings().set_bool(settings.BITWISE_MASKS,False,bv);f.reanalyze();settle(bv)
        assert text(f)==before
    finally:bv.file.close()
