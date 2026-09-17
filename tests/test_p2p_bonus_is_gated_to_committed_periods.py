"""nimbus #1079: the P2P premium is a property of the BLOCK, not the day.

Found while answering a direct household question -- "I am not satisfied
with 51%" -- about EPR on a real install, 2026-09-17.

`_compute_report_for_window()` reads the day's real settled P2P figures
and rebuilds `grid_oracle` with `export_bonus_price` so the oracle
prices export the way the settlement actually paid (#1015/#1056). It
computed the rate correctly -- `real_p2p_dollars / real_p2p_volume_kwh`
-- and then handed it to the LP as `np.full(n_periods, bonus_rate)`:
flat across all twenty-four hours, including every hour outside the
household's committed window, where a P2P scheme pays exactly nothing.

What that funds, measured on real devhub data scoring 15 Sep 2026 with
a real bonus rate of $10.4032 / 47.668 kWh = $0.2183/kWh:

    j_star charged the battery at 01:00-02:00 local, paying $0.215/kWh
    to import, then dumped 20 kW to the grid at 05:00 local into a spot
    export price of $0.088/kWh.

On its face that is a 13c/kWh loss and no optimiser takes it. It only
clears because the ungated premium turns $0.088 into $0.306. Every
dollar of it inflates `j_star`, and regret is measured against `j_star`
-- so the scorer was manufacturing regret out of a trade the household
could not have made.

The same `grid_oracle` object is reused for `j_ref` pricing (#1026), so
the reference plane was credited the premium too -- on that same day for
~41 kWh of MIDDAY SOLAR export, none of it inside the window. That lifts
`j_ref` toward `j_ach` and shrinks EPR's numerator. Both ends of the
fraction moved, both in the direction that depresses EPR.

Two further copies of the same shape were fixed with it: the
"what would Nimbus alone have done" counterfactual replay, and the
static-config fallback pricing branch -- the latter prices a LIVE
dispatch LP, so an ungated premium there buys real energy at a real
cost to chase revenue that never arrives. That branch's LocalVolts
sibling had already been fixed this way on 2026-09-05 (zero the bonus
wherever `p2p_export[i]` is 0); the fallback never got the equivalent.

The gate reuses `fetch_p2p_fixed_export_kw()` rather than reading the
block hours a second time, so it cannot drift away from the window the
LP is actually pinned to.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import numpy as np
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
DAY_START = datetime(2026, 9, 15, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)

# The real settled figures from the day this was found on.
SETTLED = {"2026-09-15": {"export_cost": 10.4032, "export_volume": 47.668}}
BONUS_RATE = 10.4032 / 47.668


class TestTheHelperItself(unittest.TestCase):
    """`p2p_bonus_price_by_period()` in isolation -- the semantics the
    three call sites all depend on."""

    def test_the_rate_is_offered_only_where_a_commitment_exists(self):
        committed = [0.0, 0.0, 12.0, 12.0, 0.0]
        out = solver_writer.p2p_bonus_price_by_period(0.25, committed, 5)
        np.testing.assert_allclose(out, [0.0, 0.0, 0.25, 0.25, 0.0])

    def test_no_configured_block_keeps_the_flat_rate_unchanged(self):
        """`None` means no block is configured at all, so there is no
        window to gate on. Falling back to flat is the pre-existing
        behaviour -- this fix must not make such an install worse on the
        strength of a guess about a window that was never described."""
        out = solver_writer.p2p_bonus_price_by_period(0.25, None, 4)
        np.testing.assert_allclose(out, [0.25] * 4)

    def test_a_zero_rate_stays_a_complete_no_op(self):
        """A household with no P2P scheme configures no bonus price. The
        gate must not turn that into anything other than all-zeros."""
        out = solver_writer.p2p_bonus_price_by_period(0.0, [0.0, 12.0], 2)
        np.testing.assert_allclose(out, [0.0, 0.0])

    def test_the_post_midnight_self_consume_pin_earns_no_bonus(self):
        """`fetch_p2p_fixed_export_kw()` returns exactly 0.0 for the
        self-consume hours after a through-midnight block closes, where
        grid export is pinned off entirely. Zero export can earn no
        premium, and the gate must agree rather than offering one."""
        committed = [12.0, 12.0, 0.0, 0.0]  # block closes, self-consume follows
        out = solver_writer.p2p_bonus_price_by_period(0.3, committed, 4)
        self.assertEqual(list(out[2:]), [0.0, 0.0])


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
        # The household's own real commitment: 17:00-24:00 local.
        "solver_p2p_block_1_rate_kw": 12.0,
        "solver_p2p_block_1_start_hour": 17,
        "solver_p2p_block_1_end_hour": 24,
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


class TestTheOracleIsNotPaidOutsideTheWindow(unittest.TestCase):
    def test_the_bonus_priced_oracle_is_gated_to_the_committed_periods(self):
        """End to end through `_compute_report_for_window()`: the
        bonus-priced `grid_oracle` must carry the settled rate in the
        committed periods and zero everywhere else.

        Asserted against the SAME call's own `fixed_export_kw`, which is
        the array the LP is genuinely pinned by -- so this cannot pass
        by agreeing with a second, independently-wrong reading of the
        block hours.
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

        self.assertEqual(report["real_p2p_settlement_status"], "applied")

        bonus_calls = [
            call
            for call in grid_config_spy.call_args_list
            if call.kwargs.get("export_bonus_price") is not None
        ]
        self.assertTrue(
            bonus_calls,
            "no bonus-priced GridConfig was built at all -- #1056's own "
            "regression, not this one.",
        )

        for call in bonus_calls:
            bonus = np.asarray(call.kwargs["export_bonus_price"], dtype=float)
            committed = call.kwargs.get("fixed_export_kw")
            self.assertIsNotNone(
                committed,
                "a configured P2P block must reach the oracle as "
                "fixed_export_kw -- without it there is no window to gate on.",
            )
            committed = np.asarray(committed, dtype=float)

            self.assertTrue(
                (bonus[committed <= 0.0] == 0.0).all(),
                "the oracle is being offered the P2P premium in periods "
                "with no commitment. That is what funded j_star charging "
                "at $0.215/kWh overnight and dumping 20 kW at 05:00 into "
                "an $0.088/kWh spot price -- a trade that only clears "
                "because of a premium the household could not have earned "
                "at that hour.\n"
                f"  bonus at uncommitted periods: "
                f"{sorted(set(bonus[committed <= 0.0].tolist()))}",
            )
            np.testing.assert_allclose(
                bonus[committed > 0.0],
                BONUS_RATE,
                err_msg=(
                    "the committed periods must still earn the real settled "
                    "rate -- gating the premium must not remove it from the "
                    "window where it is genuinely paid."
                ),
            )

    def test_the_gate_actually_bites_on_this_scenario(self):
        """A guard that cannot fail is indistinguishable from one that
        works (#757's own lesson, and the reason three separate guards
        were found toothless on 2026-09-16).

        The assertion above is vacuous unless this day genuinely has
        periods on both sides of the window, so pin that directly: a
        17:00-24:00 block over a full local day must leave real
        uncommitted periods for the premium to have been wrongly offered
        in.
        """
        grid_times = []
        t = DAY_START
        while t < DAY_END:
            grid_times.append(t)
            t += timedelta(minutes=15)
        committed = solver_writer.fetch_p2p_fixed_export_kw(_cfg(), grid_times)
        self.assertIsNotNone(committed)
        arr = np.asarray(committed, dtype=float)
        self.assertGreater((arr > 0).sum(), 0, "no committed periods to price")
        self.assertGreater(
            (arr <= 0).sum(), 0, "no uncommitted periods -- assertion is vacuous"
        )


if __name__ == "__main__":
    unittest.main()
