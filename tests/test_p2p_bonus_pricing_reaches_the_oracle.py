"""IV&V finding (since-#996 pass, 2026-09-17): #1016 moved the P2P
bonus-rate `GridConfig` rebuild into the wrong branch, silently killing
the bonus pricing it and #1026 both claim to apply.

Before #1016 (`3ae1f3d`), the block that rebuilds `grid_oracle` with
`export_bonus_price` set (a real settled $/kWh rate, so the oracle
prices export the same way the real settlement did, not at plain spot)
lived inside `if day_data:` -- the branch where `real_p2p_dollars`/
`real_p2p_volume_kwh` had JUST been assigned from real settlement data
a few lines above, so `real_p2p_volume_kwh > 0.01` meant something.

#1016 added a new `real_p2p_settlement_status` field and, in doing so,
re-indented that same block one level deeper, into a new
`elif real_p2p_settlement_status != "settlement_sensor_unreadable":`
branch -- mutually exclusive with `if day_data:`. In that `elif`,
`day_data` is falsy, so `real_p2p_dollars`/`real_p2p_volume_kwh` are
never reassigned there; they still hold their initial `0.0`. The
`> 0.01` check is therefore always False in every branch that can
reach it, and the bonus-rate `GridConfig` rebuild never runs again --
on ANY install with a settlement sensor configured, including the
"applied" (fully successful) case.

This directly contradicts #1016's own commit message ("Diagnostic
only -- no economics change") and separately makes #1026's j_ref
bonus-pricing addition (which reuses this same `grid_oracle`) dead on
arrival too, for the identical reason.

This test pins the wiring itself -- not the status string
(`test_p2p_settlement_status.py` already covers that, and continues to
pass, because the bug is invisible to it) -- by spying on
`elements.GridConfig` construction and asserting a real, non-trivial
settlement day actually produces a bonus-priced oracle.

FIXED in v0.94.366: the misplaced `if real_p2p_volume_kwh > 0.01:`
block was moved back under `if day_data:`, and this test's original
`xfail(strict=True)` marker removed. It now passes as an ordinary
regression test, which is the point -- the wiring, not the status
string, is what has to stay guarded.
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

# A real, non-trivial settlement day: enough volume that the bonus rate
# (export_cost / export_volume) is a real, distinct-from-spot number.
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


def _ha_get(entity_id):
    if entity_id == "sensor.p2p":
        return {"attributes": {"history": SETTLED}}
    return {"attributes": {"unit_of_measurement": "kW"}}


class TestP2PBonusPricingReachesTheOracle(unittest.TestCase):
    def test_a_real_settled_day_rebuilds_grid_oracle_with_a_bonus_price(self):
        """The 'applied' case (real_p2p_settlement_status == 'applied',
        real settlement volume well above the 0.01 kWh noise floor)
        must produce a GridConfig carrying the real settled bonus rate
        -- not the plain-spot GridConfig built earlier in the function.
        """
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=_fetch
            ),
            patch.object(solver_writer, "ha_get", side_effect=_ha_get),
            patch.object(
                solver_writer.elements,
                "GridConfig",
                side_effect=solver_writer.elements.GridConfig,
            ) as grid_config_spy,
        ):
            report = solver_writer._compute_report_for_window(
                _cfg(solver_p2p_settlement_history_sensor="sensor.p2p"),
                DAY_START,
                DAY_END,
                allow_partial=True,
            )

        # Sanity: the status/dollars this file's sibling test already
        # pins are unaffected by this bug (that's exactly why it hid).
        self.assertEqual(report["real_p2p_settlement_status"], "applied")
        self.assertAlmostEqual(report["real_p2p_dollars"], 10.4032, places=4)

        bonus_calls = [
            call
            for call in grid_config_spy.call_args_list
            if call.kwargs.get("export_bonus_price") is not None
        ]
        self.assertTrue(
            bonus_calls,
            "GridConfig was never constructed with export_bonus_price set, "
            "even though real_p2p_settlement_status == 'applied' with "
            f"real_p2p_volume_kwh={SETTLED['2026-09-15']['export_volume']} "
            "(well above the 0.01 kWh gate) -- the oracle is silently "
            "pricing export at plain spot instead of the real settled rate.",
        )


if __name__ == "__main__":
    unittest.main()
