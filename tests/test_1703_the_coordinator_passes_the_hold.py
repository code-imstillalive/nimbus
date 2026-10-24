"""nimbus #1703: the coordinator passes a load's running threshold and hold
to its predict call. A check that reads source text; the hold itself is in
tests/v2/forecasting/test_running_hold.py (#1837)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import _ml_path  # noqa: F401

START = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001
DAYS = 21


def _burst_load_events() -> list[tuple[datetime, float]]:
    """An AC that runs 0.9 kW for one hour at 13:00 each day, idle 0.02 kW
    otherwise -- short runs, like the real circuit (6% of time above
    0.5 kW)."""
    events = []
    t = START
    end = START + timedelta(days=DAYS)
    while t < end:
        events.append((t, 0.9 if t.hour == 13 else 0.02))
        t += timedelta(minutes=15)
    return events


class TestTheFieldsReachThePredictCall(unittest.TestCase):
    def test_the_coordinator_passes_both_fields(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "nimbus_load"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        # The call lives in the coordinator's `_predict` slice helper (the
        # grid is "now" then the clock, predicted in two parts, 13 Oct 2026).
        call = src[src.index("def _predict(lo: int, hi: int | None)") :][:1200]
        self.assertIn("self._running_threshold_kw", call)
        self.assertIn("self._run_hold_hours", call)
