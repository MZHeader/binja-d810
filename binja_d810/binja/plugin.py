"""Everything that runs when Binary Ninja loads the plugin."""

from . import test_runner, workflow


def register() -> None:
    workflow.register()
    test_runner.maybe_run_from_env()
