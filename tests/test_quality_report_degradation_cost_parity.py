"""End-to-end regression test for a real finding from Mark Purcell's live
dispatch-report tooling (nimbus issue #336, 2026-09-04 dashboard
analysis): solver_writer.py's own quality-report battery config never
populated `degradation_cost_per_kwh`, defaulting it to 0.0 -- so j_ref/
j_ach (evaluate_realized_cost()) and j_star/the oracle (build_plan())
all scored a battery that cycles for free, while the REAL live dispatch
battery config prices this field for real. For an install with it
configured nonzero, the oracle in particular over-cycled for "free"
arbitrage the household would never find worthwhile net of degradation,
inflating regret_dollars.

Fixed by threading solver_degradation_cost_per_kwh through the same
battery_cfg used for j_ref/j_ach/j_star. This test proves the fix
reaches all the way from cfg through to the published report -- not
just the pure regret.py unit level (see test_regret_degradation_cost.py
for that).

Same "_compute_report_for_window() end-to-end, real fetch mocking"
pattern as tests/test_quality_report_p2p_fixed_export.py.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ

DAY_START = datetime(2026, 8, 31, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)


def _cfg(**overrides):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_capacity_kwh": 122.2,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 40.0,
        "solver_max_discharge_kw": 40.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.0,
        "solver_grid_max_import_kw": 42.0,
        "solver_grid_max_export_kw": 42.0,
    }
    cfg.update(overrides)
    return cfg


def _flat_history(value, start, end, step_minutes=15):
    out = []
    t = start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _volatile_price_history(start, end, low, high, step_minutes=15):
    """Alternates cheap/expensive every 2 hours -- real enough price
    volatility that an unconstrained oracle genuinely wants to cycle the
    battery for arbitrage (the exact condition the issue's own finding
    depends on: "the oracle over-cycles" only matters if there's
    something worth over-cycling FOR)."""
    out = []
    t = start
    while t < end:
        block = int((t - start).total_seconds() // 3600) // 2
        out.append((t, low if block % 2 == 0 else high))
        t += timedelta(minutes=step_minutes)
    return out


def _fetch(entity_id, start, end):
    if entity_id == "sensor.real_solar":
        return _flat_history(0.0, start, end)
    if entity_id == "sensor.real_load":
        return _flat_history(2.0, start, end)
    if entity_id == "sensor.real_battery":
        # A real, nonzero, flat discharge reading -- so j_ach's own
        # throughput is genuinely nonzero (not the degenerate "achieved
        # did nothing all day" case, which would make this fix
        # invisible in j_ach even though it's still real for j_star).
        return _flat_history(1.0, start, end)
    if entity_id == "sensor.import_price":
        return _volatile_price_history(start, end, low=0.10, high=0.45)
    if entity_id == "sensor.export_price":
        return _volatile_price_history(start, end, low=0.05, high=0.30)
    return []


class TestQualityReportDegradationCostParity(unittest.TestCase):
    def _run(self, cfg):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_fetch
        ):
            return solver_writer._compute_report_for_window(
                cfg, DAY_START, DAY_END, allow_partial=False
            )

    def test_default_zero_degradation_cost_is_unaffected(self):
        """No degradation cost configured (the default) -- byte-
        identical to before this fix existed."""
        report = self._run(_cfg())
        self.assertIsNotNone(report)

    def test_j_ref_is_unaffected_by_degradation_cost(self):
        """The idle reference cycles nothing by definition -- a nonzero
        degradation_cost_per_kwh must not change j_ref at all."""
        zero_deg = self._run(_cfg(solver_degradation_cost_per_kwh=0.0))
        with_deg = self._run(_cfg(solver_degradation_cost_per_kwh=0.03))
        self.assertAlmostEqual(zero_deg["j_ref"], with_deg["j_ref"], places=6)

    def test_j_star_the_oracle_gets_more_expensive_with_real_degradation_cost(self):
        """The oracle's own LP re-solve must now price the SAME
        degradation cost the real dispatch does -- with a nonzero value
        configured, j_star (the oracle's own reported cost) must be
        higher than it was at zero degradation cost (a more expensive
        battery to cycle makes even the oracle's own optimal cost go
        up, since it now genuinely pays for the throughput it chooses)."""
        zero_deg = self._run(_cfg(solver_degradation_cost_per_kwh=0.0))
        with_deg = self._run(_cfg(solver_degradation_cost_per_kwh=0.05))
        self.assertGreater(
            with_deg["j_star"],
            zero_deg["j_star"],
            "the oracle's own reported cost did not increase once a real "
            "degradation cost was configured -- suggests the oracle's LP "
            "re-solve is still cycling the battery for free",
        )

    def test_j_ach_also_reflects_the_real_achieved_throughput_cost(self):
        """j_ach's own real, already-realized throughput must also pay
        the configured degradation cost -- both legs of the comparison
        need to price the SAME battery physics, or the resulting
        regret figure isn't comparing like with like."""
        zero_deg = self._run(_cfg(solver_degradation_cost_per_kwh=0.0))
        with_deg = self._run(_cfg(solver_degradation_cost_per_kwh=0.05))
        self.assertGreater(
            with_deg["j_ach"],
            zero_deg["j_ach"],
            "j_ach did not increase once a real degradation cost was "
            "configured, despite a genuinely nonzero real battery "
            "reading (1.0kW flat) -- the achieved leg is still scoring "
            "a free-to-cycle battery",
        )
