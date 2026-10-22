"""Exercise the real registered Plugins menu against a native analysis session."""

from types import SimpleNamespace

from binaryninja import AnalysisState, BinaryView, PluginCommand, PluginCommandContext, Settings

from helpers import function_named, open_sample, settle
from flowcases import text
from binja_d810.binja import commands, completion, settings, workflow
from binja_d810.binja.flow import apply


def context(view, func=None):
    ctx = PluginCommandContext(view)
    ctx.function = func
    return ctx


def test_function_menu_settings_report_completion_and_undo(monkeypatch):
    bv = open_sample("pointer_uses_O0.dylib", d810=False)
    try:
        func = function_named(bv, "return_read")
        before = text(func)
        ctx = context(bv, func)
        menu = PluginCommand.get_valid_list(ctx)
        names = {f"D810\\{word} for {scope}" for word in ("Enable", "Disable")
                 for scope in ("Current Function", "This Binary")} | {"D810\\Show Function Report"}
        assert names <= menu.keys()
        messages = []
        original = completion.log.logger

        def record(message):
            if "analysis complete" in message:
                messages.append((message, bv.analysis_info.state, apply.busy(bv)))
            original.log_info(message)

        monkeypatch.setattr(completion.log, "logger", SimpleNamespace(
            log_info=record, log_error=original.log_error, log_warn=original.log_warn, log_debug=original.log_debug))
        menu["D810\\Enable for Current Function"].execute(ctx)
        settle(bv)
        assert Settings().get_bool(workflow.ACTIVITY_SETTING, func)
        assert Settings().get_bool(settings.ENABLED, func)
        assert not Settings().get_bool(workflow.ACTIVITY_SETTING, bv)
        assert text(func) != before, (workflow.debug_report_text(func), apply.notes_for(bv, func.start), messages)
        assert messages and all(state == AnalysisState.IdleState and not busy for _, state, busy in messages)
        reports = []
        monkeypatch.setattr(BinaryView, "show_plain_text_report", lambda self, title, contents: reports.append((title, contents)))
        menu["D810\\Show Function Report"].execute(ctx)
        assert reports and "rewrites:" in reports[0][1]
        messages.clear()
        menu["D810\\Disable for Current Function"].execute(ctx)
        settle(bv)
        assert not Settings().get_bool(workflow.ACTIVITY_SETTING, func)
        assert text(func) == before
        assert messages and all(not busy for _, _, busy in messages)
    finally:
        bv.file.close()


def test_binary_menu_overrides_function_settings_and_restores_analysis():
    bv = open_sample("keys_O0.dylib", d810=False)
    try:
        func = function_named(bv, "key_xor")
        before = text(func)
        Settings().set_bool(workflow.ACTIVITY_SETTING, False, func)
        for enabled in (True, False):
            task = commands.binary_enabled(bv, enabled)
            task.join(30)
            assert task.finished
            settle(bv)
            assert Settings().get_bool(workflow.ACTIVITY_SETTING, bv) == enabled
            assert all(Settings().get_bool(workflow.ACTIVITY_SETTING, f) == enabled for f in bv.functions)
            assert commands._available(bv)
        assert text(func) == before
        assert not len(apply.record_of(bv))
    finally:
        bv.file.close()
