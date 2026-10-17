"""Everything that runs when Binary Ninja loads the plugin."""

from binaryninja import BinaryViewType

from . import limits, settings, test_runner, workflow


def register() -> None:
    settings.register()
    workflow.register()
    # Before a binary's analysis starts: room for D-810's budget in Binary Ninja's limit (v3.3).
    BinaryViewType.add_binaryview_finalized_event(limits.on_view_finalized)
    test_runner.maybe_run_from_env()
