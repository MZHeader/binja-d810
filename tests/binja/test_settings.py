from binaryninja import Settings
from helpers import function_named, lift_return, manifest, open_sample, set_types, signature, strip_versions

from binja_d810.binja import log, settings
from binja_d810.ir.expr import is_mba
from binja_d810.rules.registry import load_rules

# Binary Ninja leaves this one mixed at -O2; see test_engine.py.
ENTRY = next(e for e in manifest() if e["name"] == "mba_d810_Xor_HackersDelightRule_1")


def _reanalyze(bv, func):
    func.reanalyze()
    bv.update_analysis_and_wait()
    return function_named(bv, ENTRY["name"])


def _open():
    bv = open_sample("mba_O2.dylib", d810=True)
    set_types(bv, {ENTRY["name"]: signature(ENTRY)})
    return bv, function_named(bv, ENTRY["name"])


def test_settings_are_registered():
    s = Settings()
    for key in (settings.ENABLED, settings.TIER2, settings.MAX_VARS, settings.MAX_NODES,
                settings.MAX_ITERATIONS, settings.DISABLED_RULES, settings.SKIP_FUNCTIONS):
        assert s.contains(key), key


def test_the_default_budget_is_the_most_binary_ninjas_default_limit_allows():
    # 10 s: half of analysis.limits.maxFunctionAnalysisTime's default 20 s (budget.plugin_allowance).
    from binja_d810.binja.budget import plugin_allowance
    from binja_d810.binja.engine import EngineConfig

    s = Settings()
    assert s.get_integer(settings.FUNCTION_SECONDS) == 10
    assert EngineConfig().function_seconds == 10
    assert plugin_allowance(10, s.get_integer("analysis.limits.maxFunctionAnalysisTime")) == 10


def test_kill_switch():
    bv, func = _open()
    try:
        assert not is_mba(strip_versions(lift_return(func)))
        Settings().set_bool(settings.ENABLED, False, bv)
        assert is_mba(strip_versions(lift_return(_reanalyze(bv, func))))
    finally:
        bv.file.close()


def test_skip_list():
    bv, func = _open()
    try:
        Settings().set_string_list(settings.SKIP_FUNCTIONS, [f"{func.start:#x}"], bv)
        assert is_mba(strip_versions(lift_return(_reanalyze(bv, func))))
    finally:
        bv.file.close()


def test_disabling_every_rule_and_tier2_disables_rewriting():
    bv, func = _open()
    try:
        Settings().set_string_list(settings.DISABLED_RULES, [r.name for r in load_rules()], bv)
        Settings().set_bool(settings.TIER2, False, bv)
        assert is_mba(strip_versions(lift_return(_reanalyze(bv, func))))
    finally:
        bv.file.close()


def test_iteration_cap_is_reported():
    bv, func = _open()
    try:
        Settings().set_integer(settings.MAX_ITERATIONS, 1, bv)  # set_double is ignored by integer reads
        func = _reanalyze(bv, func)
        report = log.latest_report(func)
        assert report.capped and report.still_firing
        assert not is_mba(strip_versions(lift_return(func)))  # the one allowed pass still applied
    finally:
        bv.file.close()
