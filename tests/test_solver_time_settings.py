"""Household, 11 Oct 2026: the battery reserve's window is a pair of time
entities (00:00 -> 05:30), not numbers reading "0 h" and "5.5 h". The Solver
still receives hours, so the conversion must be exact both ways, and a
window ending at midnight must keep its meaning."""

from __future__ import annotations

from datetime import time
from pathlib import Path

from tests._ha_stubs import install_ha_stubs

install_ha_stubs()

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _helpers():
    src = (_PKG / "time.py").read_text(encoding="utf-8")
    start = src.index("def hours_to_time")
    end = src.index("async def async_setup_entry")
    ns: dict = {"time": time}
    exec(src[start:end], ns)  # noqa: S102 -- the pure helpers only, without HA imports
    return ns


def test_hours_and_clock_time_round_trip() -> None:
    h = _helpers()
    assert h["hours_to_time"](5.5) == time(5, 30)
    assert h["hours_to_time"](0) == time(0, 0)
    assert h["time_to_hours"](time(5, 30)) == 5.5
    assert h["state_to_hours"]("05:30:00") == 5.5
    assert h["state_to_hours"]("unknown") is None
    for half_hour in range(48):
        hours = half_hour / 2
        assert h["time_to_hours"](h["hours_to_time"](hours)) == hours


def test_a_window_ending_at_midnight_keeps_its_meaning() -> None:
    """24 becomes 00:00. The reserve window treats start > end as wrapping
    midnight, so 18 -> 24 and 18 -> 0 cover the same hours, and 0 -> 24
    (all day) becomes 0 -> 0, which is also all day."""
    h = _helpers()
    assert h["hours_to_time"](24) == time(0, 0)
    src = (_PKG / "solver_plan.py").read_text(encoding="utf-8")
    assert "start > end wraps midnight" in src
    assert "Equal hours = None (all day" in src


def test_the_old_numbers_are_gone_and_the_bridge_reads_time() -> None:
    number_src = (_PKG / "number.py").read_text(encoding="utf-8")
    assert "CONF_SOLVER_BATTERY_RESERVE_START_HOUR" not in number_src
    sensor_src = (_PKG / "sensor.py").read_text(encoding="utf-8")
    assert "_SOLVER_TIME_ENTITY_KEYS" in sensor_src
    assert 'self._entity_id_for("time", key)' in sensor_src
    init_src = (_PKG / "__init__.py").read_text(encoding="utf-8")
    assert "Platform.TIME" in init_src
