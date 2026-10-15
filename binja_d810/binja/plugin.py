"""Everything that runs when Binary Ninja loads the plugin."""

from . import settings, test_runner, workflow


def register() -> None:
    settings.register()
    workflow.register()
    test_runner.maybe_run_from_env()
