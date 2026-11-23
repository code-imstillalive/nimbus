"""HACS review (Frenck, 23 Nov 2026): set_default_env_vars() calls
tempfile.gettempdir(), whose first call in a process writes and deletes a
probe file. The sensor platform's setup ran it on the event loop; it must go
through the executor like the solver_writer import below it."""

from __future__ import annotations

import re
from pathlib import Path

SENSOR = (
    Path(__file__).resolve().parents[3]
    / "custom_components"
    / "nimbus_load"
    / "sensor.py"
)


def test_set_default_env_vars_runs_in_the_executor_not_on_the_loop():
    src = SENSOR.read_text(encoding="utf-8")
    assert "async_add_executor_job(solver_runtime.set_default_env_vars" in src
    direct = re.findall(
        r"^\s*solver_runtime\.set_default_env_vars\(hass\)", src, re.MULTILINE
    )
    assert direct == []
