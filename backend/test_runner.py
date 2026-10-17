"""Test runner: the only part of Traceback that executes the project's code.

Runs pytest in a given folder with a fixed command and returns a structured
result. Never accepts a command from a model. The API key is removed from
the child environment, so agent-written tests never see it.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config

OUTCOME_BY_EXIT_CODE = {0: "passed", 1: "failed", 2: "error", 3: "error", 4: "error", 5: "no_tests"}


def clean_caches(folder):
    folder = Path(folder)
    for name in ("__pycache__", ".pytest_cache"):
        for p in list(folder.rglob(name)):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)


def _count(pattern, text):
    m = re.findall(pattern, text)
    return int(m[-1]) if m else 0


def run_pytest(target, test_file=None, *, evidence=None, label="tests", phase=""):
    """Run pytest in `target`. Returns (result dict, full output text)."""
    target = Path(target).resolve()
    clean_caches(target)

    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           "--rootdir", str(target)]
    if test_file:
        cmd.append(test_file)

    env = dict(os.environ)
    env.pop(config.API_KEY_ENV_VAR, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=target, env=env, capture_output=True, text=True,
            timeout=config.TEST_TIMEOUT_SECONDS,
        )
        exit_code = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
        outcome = OUTCOME_BY_EXIT_CODE.get(exit_code, "error")
        # Exit code 1 without any failed or errored test means pytest itself
        # did not run properly (for example, pytest not installed): not a result.
        if outcome == "failed" and _count(r"(\d+) failed", output) == 0 \
                and _count(r"(\d+) errors?", output) == 0:
            outcome = "error"
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        output = out + err + f"\n[Traceback: killed after {config.TEST_TIMEOUT_SECONDS} seconds]"
        outcome = "timeout"
    duration = round(time.monotonic() - start, 2)

    result = {
        "target": str(target),
        "test_file": test_file or "(whole suite)",
        "exit_code": exit_code,
        "outcome": outcome,
        "passed_count": _count(r"(\d+) passed", output),
        "failed_count": _count(r"(\d+) failed", output),
        "duration_seconds": duration,
        "evidence_id": None,
    }
    if evidence is not None:
        header = (f"command: python -m pytest -q -p no:cacheprovider --rootdir <target>"
                  f"{' ' + test_file if test_file else ''}\n"
                  f"target: {target}\nexit_code: {exit_code}\noutcome: {outcome}\n"
                  f"duration_seconds: {duration}\n\n===== OUTPUT =====\n")
        result["evidence_id"] = evidence.save(label, phase, header + output)
    return result, output


def has_assertion_failure(output):
    return "AssertionError" in output or "assert" in output