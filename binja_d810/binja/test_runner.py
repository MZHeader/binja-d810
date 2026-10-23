"""Runs pytest inside the Binary Ninja GUI process (Personal edition has no headless API).

scripts/binja_pytest.sh launches Binary Ninja with BINJA_D810_PYTEST_ARGS (JSON list of pytest
arguments), BINJA_D810_PYTEST_OUT (a directory) and BINJA_D810_PYTEST_CWD (where pytest runs). This module runs pytest on a background
thread, writes pytest.log and result.json into that directory, then quits the application. On macOS the
test process prevents idle sleep and, on AC power, system sleep for its lifetime. Native analysis
timers can count sleep that Python's monotonic test timer excludes.
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ENV_ARGS = "BINJA_D810_PYTEST_ARGS"
ENV_OUT = "BINJA_D810_PYTEST_OUT"
ENV_CWD = "BINJA_D810_PYTEST_CWD"  # the directory pytest runs in (an app launched hidden starts in /)
ENV_TIMEOUT = "BINJA_D810_PYTEST_TIMEOUT"  # bounds the idle-sleep assertion even if the launcher disappears


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
    awake = None
    try:
        if sys.platform == "darwin":
            awake = subprocess.Popen(["/usr/bin/caffeinate", "-is", "-t", os.environ.get(ENV_TIMEOUT, "3600"),
                                      "-w", str(os.getpid())],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        import pytest

        if os.environ.get(ENV_CWD):
            os.chdir(os.environ[ENV_CWD])
        with open(out_dir / "pytest.log", "w") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            exit_code = int(pytest.main(args))
    finally:
        (out_dir / "result.json").write_text(json.dumps({"exit_code": exit_code}))
        if awake is not None:
            awake.terminate()
            try:
                awake.wait(timeout=5)
            except subprocess.TimeoutExpired:
                awake.kill()
                awake.wait()
        _quit()


def _quit() -> None:
    try:
        from binaryninja.mainthread import execute_on_main_thread
        from PySide6.QtWidgets import QApplication

        execute_on_main_thread(QApplication.instance().quit)
    except Exception:
        os._exit(0)
