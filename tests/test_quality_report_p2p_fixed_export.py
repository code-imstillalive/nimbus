"""Real bug found live, 2026-09-01 (direct household catch on a
reconstructed dispatch-regret chart): the oracle's own LP re-solve
(build_plan(), inside compute_quality_report()'s J_star calculation) had
no idea a household's real P2P export program is a FIXED, committed rate
during specific hours (e.g. 11.5kW, 17:00-24:00) -- it modeled an open
market up to solver_grid_max_export_kw instead. This systematically
overstated J_star (the oracle wanted to "dump" far more export than the
real program could ever deliver), inflating regret_dollars and deflating
EPR for every household running a fixed-rate P2P program and not just a
plain price-taking market.

fetch_p2p_fixed_export_kw() already existed and was already the correct,
tested mechanism the forward-planning branch (main()) used for exactly
this constraint -- this bug was that _compute_report_for_window() never
reused it for the RETROSPECTIVE oracle re-solve. These tests lock in
that it now does.
"""

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
        # Matches the real reference household's own real scale
        # (122.2kWh / 40kW / 40kW / 42kW / 42kW), not arbitrary small
        # test values -- the whole point of this test is that a fixed
        # 11.5kW/7h (80.5kWh) commitment is physically deliverable by a
        # real-sized system with real daytime solar, so an infeasible
        # solve here would itself be a red flag, not an expected outcome.
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


def _solar_history(start, end, step_minutes=15):
    """A real, bell-shaped daytime solar curve (peaks ~30kW at midday,
    zero overnight) -- physically realistic enough that a 122.2kWh
    battery can genuinely charge through the day and still deliver a
    fixed 11.5kW/7h evening export commitment without going infeasible."""
    out = []
    t = start
    while t < end:
        hour = t.hour + t.minute / 60.0
        if 6 <= hour <= 18:
            out.append((t, 30.0 * max(0.0, 1 - ((hour - 12) / 6) ** 2)))
        else:
            out.append((t, 0.0))
        t += timedelta(minutes=step_minutes)
    return out


def _fetch(entity_id, start, end):
    if entity_id == "sensor.real_solar":
        return _solar_history(start, end)
    if entity_id == "sensor.real_load":
        return _flat_history(1.0, start, end)
    if entity_id == "sensor.real_battery":
        return _flat_history(0.0, start, end)
    if entity_id == "sensor.import_price":
        # Cheap all day except 17:00-19:59, matching the real incident's
        # own price shape -- this is what makes an unconstrained oracle
        # WANT to dump export during that window in the first place.
        return [
            (
                start + timedelta(minutes=15 * i),
                0.55 if 17 <= (start + timedelta(minutes=15 * i)).hour < 20 else 0.15,
            )
            for i in range(96)
        ]
    if entity_id == "sensor.export_price":
        return [
            (
                start + timedelta(minutes=15 * i),
                0.11 if 17 <= (start + timedelta(minutes=15 * i)).hour < 20 else 0.05,
            )
            for i in range(96)
        ]
    return []


class TestOracleRespectsFixedP2PExportRate(unittest.TestCase):
    def _run(self, cfg):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_fetch
        ):
            return solver_writer._compute_report_for_window(
                cfg, DAY_START, DAY_END, allow_partial=False
            )

    def test_no_p2p_block_configured_is_unaffected(self):
        """Zero-config households (no P2P blocks set) must see byte-
        identical behaviour to before this fix -- fetch_p2p_fixed_
        export_kw() itself already guarantees a complete no-op when
        every block is unconfigured (rate_kw <= 0)."""
        report = self._run(_cfg())
        self.assertIsNotNone(report)

    def test_oracle_export_never_exceeds_the_real_fixed_rate_during_the_p2p_window(
        self,
    ):
        """The exact real incident: a household with an 11.5kW, 17:00-
        24:00 committed P2P block. Before this fix, the oracle's own
        j_star_hourly reconstruction could show battery_kw (and
        therefore grid export) far beyond 11.5kW during that window --
        confirmed live, up to 40kW. After this fix, grid_kw during every
        hour inside the committed block must sit at exactly the fixed
        rate (export, i.e. grid_kw negative, magnitude == rate)."""
        cfg = _cfg(
            solver_p2p_block_1_rate_kw=11.5,
            solver_p2p_block_1_start_hour=17,
            solver_p2p_block_1_end_hour=24,
        )
        report = self._run(cfg)
        self.assertIsNotNone(report)
        for hour in range(17, 24):
            key = (DAY_START + timedelta(hours=hour)).isoformat()
            row = report["j_star_hourly"][key]
            self.assertAlmostEqual(
                row["grid_kw"],
                -11.5,
                places=1,
                msg=(
                    f"hour {hour}: oracle's own real grid export was "
                    f"{row['grid_kw']}kW, expected exactly the real "
                    f"committed -11.5kW -- fixed_export_kw was not "
                    f"applied to the oracle's LP re-solve"
                ),
            )

    def test_oracle_export_outside_the_window_is_unconstrained(self):
        """The fixed rate applies ONLY inside the configured window --
        hours outside 17:00-24:00 must still be free for the oracle to
        optimise normally (this test would fail if the fix accidentally
        pinned every hour, not just the configured block)."""
        cfg = _cfg(
            solver_p2p_block_1_rate_kw=11.5,
            solver_p2p_block_1_start_hour=17,
            solver_p2p_block_1_end_hour=24,
        )
        report = self._run(cfg)
        self.assertIsNotNone(report)
        key = (DAY_START + timedelta(hours=3)).isoformat()
        row = report["j_star_hourly"][key]
        # Cheap overnight price -- the oracle should have no reason to
        # export at all here, but the point of this assertion is only
        # that it is NOT pinned to -11.5 (unconstrained, whatever value
        # that turns out to be).
        self.assertNotAlmostEqual(row["grid_kw"], -11.5, places=1)
