"""A configured P2P settlement sensor that was skipped says why, never reads as
an ordinary zero (#1015) (#1837).
"""

from __future__ import annotations

import contextlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import solver_shared
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


def _run(cfg, start, end, *, history=SETTLED, raises=False, branch_may_fire=True):
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

    # nimbus issue #1301 (spec 001): _kw_scale_factor()'s own internal
    # ha_get(...) call for the three power sensors now resolves from
    # solver_shared's module globals, not solver_writer's (nimbus issue
    # #861) -- this is universal, so solver_shared.ha_get is always
    # mocked. The P2P branch's own direct ha_get("sensor.p2p") call still
    # lives in _compute_report_for_window() (solver_writer.py, unmoved),
    # so solver_writer.ha_get is ALSO needed, but only for callers whose
    # own (cfg, start, end) can actually reach that branch --
    # `branch_may_fire=False` callers (a non-calendar-day window, or no
    # sensor configured at all) skip it, confirmed against this repo's
    # own noop_patches gate to establish nothing the real call graph
    # cannot reach.
    patches = [
        patch.object(solver_writer, "fetch_entity_history_range", side_effect=_fetch),
        patch.object(solver_shared, "ha_get", side_effect=_ha_get),
    ]
    if branch_may_fire:
        patches.append(patch.object(solver_writer, "ha_get", side_effect=_ha_get))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return solver_writer._compute_report_for_window(
            cfg, start, end, allow_partial=True
        )


class TestP2PSettlementStatusIsReported(unittest.TestCase):
    def test_a_non_calendar_window_is_named_not_silently_zeroed(self):
        # test_a_non_calendar_window_is_named_not_silently_zeroed: The #1015 case. Same day, same settlement data, a window that
        shifted = DAY_START + timedelta(hours=10)
        report = _run(
            _cfg(solver_p2p_settlement_history_sensor="sensor.p2p"),
            shifted,
            shifted + timedelta(days=1),
            branch_may_fire=False,
        )
        self.assertEqual(
            report["real_p2p_settlement_status"],
            "window_is_not_one_local_calendar_day",
            "a skipped settlement must be distinguishable from a "
            "household that genuinely earned no P2P -- that ambiguity "
            "hid a $10.40/day difference",
        )
        self.assertEqual(report["real_p2p_dollars"], 0.0)

        # test_an_unreadable_sensor_is_distinct_again: A configuration or connectivity fault, which is actionable in
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


if __name__ == "__main__":
    unittest.main()
