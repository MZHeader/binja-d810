"""The Plugins > D810 menu. Analysis stays asynchronous so the UI remains usable."""

import threading

from binaryninja import BackgroundTaskThread, PluginCommand, Settings

from . import completion, log, settings, workflow

_REGISTERED = False
_LOCK = threading.Lock()
_TASKS = {}


def function_enabled(view, func, enabled):
    with completion.batch(view):
        prefs = Settings()
        if enabled:
            prefs.set_bool(settings.ENABLED, True, func)
        prefs.set_bool(workflow.ACTIVITY_SETTING, enabled, func)
        log.logger.log_info(f"D810: {'enabling' if enabled else 'disabling'} {func.name}.")
        func.reanalyze()
        view.update_analysis()


class BinaryAction(BackgroundTaskThread):
    def __init__(self, view, enabled):
        super().__init__(f"D810: {'enabling' if enabled else 'disabling'} analysis")
        self.view, self.enabled = view, enabled

    def run(self):
        try:
            with completion.batch(self.view):
                prefs = Settings()
                if self.enabled:
                    prefs.set_bool(settings.ENABLED, True, self.view)
                prefs.set_bool(workflow.ACTIVITY_SETTING, self.enabled, self.view)
                for func in self.view.functions:
                    if self.enabled:
                        prefs.set_bool(settings.ENABLED, True, func)
                    prefs.set_bool(workflow.ACTIVITY_SETTING, self.enabled, func)
                    func.reanalyze()
                self.view.update_analysis()
        except Exception as exc:
            log.logger.log_error(f"D810 menu action failed: {exc!r}")
        finally:
            with _LOCK:
                _TASKS.pop(self.view.file.session_id, None)


def binary_enabled(view, enabled):
    with _LOCK:
        key = view.file.session_id
        if key in _TASKS:
            return
        task = BinaryAction(view, enabled)
        _TASKS[key] = task
    task.start()
    return task


def show_report(view, func):
    text = workflow.debug_report_text(func) or "No D810 report yet. Enable D810 for this function to run analysis."
    view.show_plain_text_report(f"D810: {func.name}", text)


def _available(view, func=None):
    with _LOCK:
        return view.file.session_id not in _TASKS


def register():
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    for enabled, word in ((True, "Enable"), (False, "Disable")):
        PluginCommand.register_for_function(
            f"D810\\{word} for Current Function", f"{word} D810 and reanalyze the current function.",
            lambda view, func, enabled=enabled: function_enabled(view, func, enabled), _available)
        PluginCommand.register(
            f"D810\\{word} for This Binary", f"{word} D810 and reanalyze this binary's functions.",
            lambda view, enabled=enabled: binary_enabled(view, enabled), _available)
    PluginCommand.register_for_function("D810\\Show Function Report", "Show the latest D810 analysis report.", show_report)
