"""Run one golden-master scenario through the real ``solver_writer.main()``.

Spec 000 Part A. Each scenario runs in a fresh interpreter (see
``run_isolated``), for three reasons measured on ``main`` at ``d5b044b``:

- ``solver_writer`` holds module-level state across calls
  (``_LAST_KNOWN_QUALITY_HISTORY``, ``_lex_calibration_failed_until`` in
  ``solver/lp.py``, the lazily cached token, the notified-once sets), so two
  scenarios in one process could see each other.
- The existing stub suite is order dependent: at ``d5b044b``,
  ``test_solver_writer_controllable_loads.py`` run before
  ``test_commanded_state_guard_reports_its_own_failure.py`` fails 35 tests
  that pass in the other order. A golden master must not inherit that.
- The floating-point pins below have to be in the environment before numpy
  is imported, which a subprocess guarantees and a shared pytest process
  cannot.

Only Home Assistant's REST API is faked (``fake_ha.FakeHA``). The clock is
frozen with freezegun, which Nimbus already uses through pytest-freezer.
State files go to a private temporary directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "custom_components" / "nimbus_load"
TESTS = REPO / "tests"

# Floating-point environment, pinned before numpy loads (same pins as
# purcell-lab/nem_pd7day's tests/conftest.py). One OpenBLAS thread, the
# Haswell (AVX2) kernel, numpy capped at X86_V3.
NUMERIC_ENV = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_CORETYPE": "Haswell",
    "NPY_DISABLE_CPU_FEATURES": "X86_V4",
    "PYTHONHASHSEED": "0",
}

# The CPython minor version snapshots are recorded on: CI's Unit Tests job
# (.github/workflows/ci.yml, python-version "3.14") and pyproject's
# requires-python floor. test_golden_master names a mismatch first in its
# failure message; it is not a hard check.
RECORDED_PYTHON = (3, 14)


def child_env(workdir: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("HA_", "NIMBUS_", "SUPERVISOR_"))
    }
    env.update(NUMERIC_ENV)
    env.update(
        {
            # Only tests/ here. custom_components/nimbus_load has a
            # select.py, and on PYTHONPATH it would shadow the stdlib
            # select module wherever that is not built in (it is on
            # some local builds, not on CI's). child.py adds the
            # package after the stdlib has loaded, as pytest does.
            "PYTHONPATH": str(TESTS),
            "GOLDEN_PKG": str(PKG),
            "TZ": "UTC",
            "HA_BASE": "http://golden.invalid:8123",
            "HA_TOKEN": "golden-master",
            "HA_TOKEN_PATH": str(workdir / "token"),
            "NIMBUS_SOLVER_TIMEZONE": "Australia/Brisbane",
            "NIMBUS_SOLVER_PLAN_STATE_PATH": str(workdir / "plan_state.json"),
            "NIMBUS_SOLVER_LOCK_PATH": str(workdir / "writer.lock"),
            "NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_PATH": str(workdir / "load_err.txt"),
            "NIMBUS_SOLVER_SOLAR_DELIVERY_RATIO_PATH": str(
                workdir / "solar_delivery.json"
            ),
            "GOLDEN_WORKDIR": str(workdir),
        }
    )
    return env


def run_isolated(name: str, cycles: int | None = None) -> dict[str, Any]:
    """Run scenario ``name`` in a fresh interpreter and return its record.

    ``cycles`` defaults to the scenario's own ``cycles``.
    """
    if cycles is None:
        from golden.scenarios import get

        cycles = get(name).cycles
    with tempfile.TemporaryDirectory(prefix=f"golden-{name}-") as tmp:
        workdir = Path(tmp)
        out = workdir / "record.json"
        proc = subprocess.run(
            [sys.executable, "-m", "golden.child", name, str(out), str(cycles)],
            cwd=REPO,
            env=child_env(workdir),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"golden scenario {name!r} failed in its child process:\n"
                f"--- stdout ---\n{proc.stdout[-4000:]}\n"
                f"--- stderr ---\n{proc.stderr[-8000:]}"
            )
        return json.loads(out.read_text(encoding="utf-8"))
