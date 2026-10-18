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


LIMIT = "analysis.limits.maxFunctionAnalysisTime"


def test_the_default_budget_is_20_seconds_with_room_in_binary_ninjas_limit():
    # v3.3: 20 s. Binary Ninja defers a function whose analysis exceeds its own limit (20 s by default),
    # so the budget stays at most half of it (budget.plugin_allowance), and a binary opened with D-810
    # enabled gets a limit of twice the budget before its analysis starts.
    from binja_d810.binja.engine import EngineConfig

    s = Settings()
    assert s.get_integer(settings.FUNCTION_SECONDS) == 20
    assert EngineConfig().function_seconds == 20
    bv, func = _open()
    try:
        assert s.get_integer(LIMIT, bv) == 40_000
        report = log.latest_reports(func)["mlil"]
        assert 19.9 < report.allowance_seconds <= 20.0, report.allowance_seconds
    finally:
        bv.file.close()


def test_a_limit_raised_after_the_analysis_started_never_raises_the_budget():
    # Binary Ninja analyzes a binary with the limit it had when the binary was opened (measured: 40 s set
    # afterwards, on the binary and the function, still deferred tigress_vm's 0x401159 at 20 s). The
    # budget is at most half of that limit, whatever the setting says later.
    from binja_d810.binja import workflow

    s = Settings()
    bv = open_sample("mba_O2.dylib", d810=False)
    try:
        set_types(bv, {ENTRY["name"]: signature(ENTRY)})
        func = function_named(bv, ENTRY["name"])
        s.set_integer(LIMIT, 40_000, bv)
        s.set_bool(workflow.ACTIVITY_SETTING, True, func)
        func = _reanalyze(bv, func)
        assert 9.9 < log.latest_reports(func)["mlil"].allowance_seconds <= 10.0
    finally:
        bv.file.close()


def test_a_lower_budget_or_a_higher_limit_is_left_alone():
    s = Settings()
    s.set_integer(settings.FUNCTION_SECONDS, 5)
    try:
        bv, _ = _open()
        assert s.get_integer(LIMIT, bv) == 20_000  # 2 x 5 s fits
        bv.file.close()
    finally:
        s.reset(settings.FUNCTION_SECONDS)
    bv = open_sample("mba_O2.dylib", d810=True, options={LIMIT: 90_000})
    try:
        assert s.get_integer(LIMIT, bv) == 90_000
    finally:
        bv.file.close()
    bv = open_sample("mba_O2.dylib", d810=True, options={LIMIT: 0})  # 0: no limit
    try:
        assert s.get_integer(LIMIT, bv) == 0
    finally:
        bv.file.close()


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


def test_the_flow_settings_are_registered_with_their_defaults():
    s = Settings()
    assert s.contains(settings.FLOW_FIXES) and s.get_bool(settings.FLOW_FIXES)
    assert s.contains(settings.READ_ONLY_RANGES) and s.get_string_list(settings.READ_ONLY_RANGES) == []
