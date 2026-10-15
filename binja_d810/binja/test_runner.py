"""Runs pytest inside the Binary Ninja GUI process (Personal edition has no headless API).

scripts/binja_pytest.sh launches Binary Ninja with BINJA_D810_PYTEST_ARGS (JSON list of pytest
arguments) and BINJA_D810_PYTEST_OUT (a directory). This module runs pytest on a background
thread, writes pytest.log and result.json into that directory, then quits the application.
"""

import contextlib
import json
import os
import threading
from pathlib import Path

ENV_ARGS = "BINJA_D810_PYTEST_ARGS"
ENV_OUT = "BINJA_D810_PYTEST_OUT"


def maybe_run_from_env() -> bool:
    raw = os.environ.get(ENV_ARGS)
    if not raw:
        return False
    out_dir = Path(os.environ[ENV_OUT])
    threading.Thread(
        target=_run, args=(json.loads(raw), out_dir), daemon=True, name="binja-d810-pytest"
    ).start()
    return True


def _run(args: list[str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    exit_code = 99
    try:
        import pytest

        with open(out_dir / "pytest.log", "w") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            exit_code = int(pytest.main(args))
    finally:
        (out_dir / "result.json").write_text(json.dumps({"exit_code": exit_code}))
        _quit()


def _quit() -> None:
    try:
        from binaryninja.mainthread import execute_on_main_thread
        from PySide6.QtWidgets import QApplication

        execute_on_main_thread(QApplication.instance().quit)
    except Exception:
        os._exit(0)
