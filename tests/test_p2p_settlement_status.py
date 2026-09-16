"""nimbus issue #1015 -- a configured P2P settlement sensor that gets
SKIPPED produced a silently ordinary-looking zero.

`_compute_report_for_window()` only looks up real settled P2P when the
window is exactly one local calendar day, because the settlement history
is keyed by ISO local date and any other window genuinely has no entry to
find. That gate is correct. Saying nothing about it was not.

Without a status, the report simply carried `real_p2p_dollars: 0` and
priced export at plain spot -- indistinguishable from a household that
earns no P2P at all. No error, no flag, an ordinary-looking number.

Measured on the reference household: scoring 2026-09-15 as a UTC-aligned
24-hour window returned `real_p2p_dollars: 0` against **$10.4032** for
the same day scored as a local calendar day, moving `j_ach` by **$9.82**.
That is the entire P2P income for the day, vanishing on a window choice,
with nothing in the output to say so.

These tests pin the status through every real outcome.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
DAY_START = datetime(2026, 9, 15, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)

SETTLED = {"2026-09-15": {"export_cost": 10.4032, "export_volume": 47.668}}


def _cfg(**overrides):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_soc_sensor": "sensor.real_soc",
        "solver_battery_capacity_kwh": 122.2,
        "solver_battery_min_soc_percent": 2.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 40.0,
        "solver_max_discharge_kw": 40.0,
        "solver_efficiency_percent": 85.8,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.0,
        "solver_grid_max_import_kw": 42.0,
        "solver_grid_max_export_kw": 42.0,
    }
    cfg.update(overrides)
    return cfg


def _flat(value, start, end, step_minutes=15):
    out, t = [], start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _fetch(entity_id, start, end):
    if entity_id == "sensor.real_solar":
        return _flat(0.0, start, end)
    if entity_id == "sensor.real_load":
        return _flat(1.0, start, end)
    if entity_id == "sensor.real_battery":
        return _flat(0.5, start, end)
    if entity_id == "sensor.import_price":
        return _flat(0.30, start, end)
    if entity_id == "sensor.export_price":
        return _flat(0.05, start, end)
    if entity_id == "sensor.real_soc":
        return _flat(50.0, start, end)
    return []


def _run(cfg, start, end, *, history=SETTLED, raises=False):
    def _ha_get(entity_id):
        # Only the settlement sensor misbehaves. `ha_get` is also used
        # by _kw_scale_factor() for unit lookups on every power sensor,
        # so raising indiscriminately blows up long before the branch
        # under test -- which is exactly what the first draft did.
        if entity_id == "sensor.p2p":
            if raises:
                raise KeyError("boom")
            return {"attributes": {"history": history}}
        return {"attributes": {"unit_of_measurement": "kW"}}

    with (
        patch.object(solver_writer, "fetch_entity_history_range", side_effect=_fetch),
        patch.object(solver_writer, "ha_get", side_effect=_ha_get),
    ):
        return solver_writer._compute_report_for_window(
            cfg, start, end, allow_partial=True
        )


class TestP2PSettlementStatusIsReported(unittest.TestCase):
    def test_no_sensor_configured_says_so(self):
        """A household with no P2P program at all. Zero is the right
        answer, and the status distinguishes it from a skip."""
        report = _run(_cfg(), DAY_START, DAY_END)
        self.assertEqual(report["real_p2p_settlement_status"], "no_sensor_configured")
        self.assertEqual(report["real_p2p_dollars"], 0.0)

    def test_a_real_calendar_day_applies_the_settlement(self):
        report = _run(
            _cfg(solver_p2p_settlement_history_sensor="sensor.p2p"),
            DAY_START,
            DAY_END,
        )
        self.assertEqual(report["real_p2p_settlement_status"], "applied")
        self.assertAlmostEqual(report["real_p2p_dollars"], 10.4032, places=4)

    def test_a_non_calendar_window_is_named_not_silently_zeroed(self):
        """The #1015 case. Same day, same settlement data, a window that
        does not start at local midnight -- the lookup genuinely cannot
        happen, and the report must say which of the two zeros this is."""
        shifted = DAY_START + timedelta(hours=10)
        report = _run(
            _cfg(solver_p2p_settlement_history_sensor="sensor.p2p"),
            shifted,
            shifted + timedelta(days=1),
        )
        self.assertEqual(
            report["real_p2p_settlement_status"],
            "window_is_not_one_local_calendar_day",
            "a skipped settlement must be distinguishable from a "
            "household that genuinely earned no P2P -- that ambiguity "
            "hid a $10.40/day difference",
        )
        self.assertEqual(report["real_p2p_dollars"], 0.0)

    def test_a_short_window_is_also_named(self):
        """Not only midnight-alignment: `allow_partial` windows shorter
        than 24 h hit the same gate."""
        report = _run(
            _cfg(solver_p2p_settlement_history_sensor="sensor.p2p"),
            DAY_START,
            DAY_START + timedelta(hours=6),
        )
        self.assertEqual(
            report["real_p2p_settlement_status"],
            "window_is_not_one_local_calendar_day",
        )

    def test_a_date_missing_from_the_table_is_distinct_from_a_skip(self):
        """A genuinely unsettled day -- the sensor read fine and simply
        has no row yet. Different cause, different remedy, so a
        different status."""
        report = _run(
            _cfg(solver_p2p_settlement_history_sensor="sensor.p2p"),
            DAY_START,
            DAY_END,
            history={"2026-01-01": {"export_cost": 1.0, "export_volume": 1.0}},
        )
        self.assertEqual(
            report["real_p2p_settlement_status"],
            "no_settlement_entry_for_this_date",
        )

    def test_an_unreadable_sensor_is_distinct_again(self):
        """A configuration or connectivity fault, which is actionable in
        a way the other zeros are not."""
        report = _run(
            _cfg(solver_p2p_settlement_history_sensor="sensor.p2p"),
            DAY_START,
            DAY_END,
            raises=True,
        )
        self.assertEqual(
            report["real_p2p_settlement_status"],
            "settlement_sensor_unreadable",
        )

    def test_every_status_is_one_of_the_documented_values(self):
        """Guards against a typo'd status string reaching a dashboard,
        where it would read as a new and meaningless category."""
        allowed = {
            "applied",
            "no_sensor_configured",
            "window_is_not_one_local_calendar_day",
            "no_settlement_entry_for_this_date",
            "settlement_sensor_unreadable",
        }
        for label, kwargs in (
            ("no sensor", {}),
            ("applied", {"solver_p2p_settlement_history_sensor": "sensor.p2p"}),
        ):
            with self.subTest(case=label):
                report = _run(_cfg(**kwargs), DAY_START, DAY_END)
                self.assertIn(report["real_p2p_settlement_status"], allowed)


if __name__ == "__main__":
    unittest.main()
