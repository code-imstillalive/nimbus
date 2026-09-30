"""nimbus issue #768: what a Controllable Load really delivered, hour by
hour, on an already-elapsed day.

That issue named this as its own blocking prerequisite on 2026-09-13 --
*"Reconstructing real historical controllable-load delivery is itself a new
capability"* -- and it stayed missing for a fortnight while everything
downstream of it waited. These tests drive the capability itself:
`solver_inputs/controllable_load_history.py`.

## What is deliberately NOT asserted here

That anything reaches the oracle. It does not, on purpose, and
`test_768_delivery_is_published_without_moving_the_score.py` pins that it
does not. Mark Purcell's own sequencing, 2026-09-27: build the
reconstruction, land it as its own change, wire the LP plumbing second.

## The three claims worth a test each

1. **The number is real.** A hand-computable power series integrates to the
   hand-computed kWh, so the test would fail on an off-by-one period or a
   scale error rather than merely on a crash.
2. **A recorder gap credits nothing.** `resample_history_mean()` carries the
   last reading forward indefinitely -- correct for a STATE, energy-
   fabricating for POWER. `load_run_state.MAX_SAMPLE_GAP_HOURS` is this
   repo's own answer to "how long may a power reading speak for", and #1161
   already taught the battery reconstruction to honour it. This one reuses
   the same helper, and the test proves it by MOVING the constant rather
   than by restating its value.
3. **Absence is absence.** A load with no power sensor, or a day before the
   sensor existed, reports `delivered_kwh = None` with a reason -- never
   `0.0`. A zero is a claim ("this load did nothing") that happens to be
   checkable and wrong, which is the shape of defect this repo keeps paying
   for (#118, #933).
"""

from __future__ import annotations

import contextlib
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import load_run_state
import solver_writer
from solver_inputs import controllable_load_history as clh

BRISBANE = solver_writer.LOCAL_TZ
DAY_START = datetime(2026, 9, 20, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)
GRID = [DAY_START + timedelta(hours=i) for i in range(24)]
PERIOD_H = 1.0

_HWS = {
    "controllable_load_name": "Hot water",
    "controllable_load_kind": "deferrable",
    "controllable_load_device_entity": "water_heater.wwk302",
    "controllable_load_power_sensor": "sensor.wwk302_power",
    "deferrable_max_power_kw": 0.65,
    "deferrable_target_kwh": 4.0,
}
_POOL = {
    "controllable_load_name": "Pool pump",
    "controllable_load_kind": "sheddable",
    "controllable_load_device_entity": "switch.pool_pump",
    "controllable_load_power_sensor": "sensor.pool_pump_power",
    "sheddable_nominal_kw": 1.2,
}


def _subentry(subentry_id, data, subentry_type="controllable_load"):
    return SimpleNamespace(
        subentry_id=subentry_id, subentry_type=subentry_type, data=data
    )


def _native_hass(subentries):
    entry = SimpleNamespace(
        entry_id="hub", subentries={s.subentry_id: s for s in subentries}
    )
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _domain: [entry]),
        states=SimpleNamespace(get=lambda _eid: None),
    )


@contextlib.contextmanager
def _power_history(rows_by_entity):
    def _fetch(entity_id, _start, _end):
        return rows_by_entity.get(entity_id, [])

    with patch.object(
        solver_writer, "fetch_entity_power_history_kw", side_effect=_fetch
    ):
        yield


def _hourly(values, step_minutes=10):
    """A sample every `step_minutes` through each hour, at that hour's own
    value -- dense enough that no period is stale under any guard."""
    out = []
    for hour, kw in enumerate(values):
        t = DAY_START + timedelta(hours=hour)
        while t < DAY_START + timedelta(hours=hour + 1):
            out.append((t, kw))
            t += timedelta(minutes=step_minutes)
    return out


def _resolve():
    return clh.resolve_controllable_load_delivery_history(
        day_start=DAY_START,
        day_end=DAY_END,
        grid_times=GRID,
        period_hours=PERIOD_H,
        n_periods=len(GRID),
    )


class _NativeModeCase(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer.NATIVE.hass

    def tearDown(self):
        solver_writer.NATIVE.hass = self._orig


class TestTheIntegratorItself(unittest.TestCase):
    """Pure arithmetic, no hass and no recorder -- so the guard can be
    tested against a hand-written series rather than only through a mock."""

    def test_a_flat_series_integrates_to_the_hand_computed_total(self):
        by_period, total = clh.integrate_delivered_kwh([2.0] * 24, 1.0, set())
        self.assertEqual(len(by_period), 24)
        self.assertAlmostEqual(total, 48.0, places=9)

    def test_period_hours_scales_the_result(self):
        _, total = clh.integrate_delivered_kwh([2.0] * 24, 0.25, set())
        self.assertAlmostEqual(total, 12.0, places=9)

    def test_a_stale_period_contributes_nothing(self):
        by_period, total = clh.integrate_delivered_kwh([2.0] * 4, 1.0, {1, 2})
        self.assertEqual(by_period, (2.0, 0.0, 0.0, 2.0))
        self.assertAlmostEqual(total, 4.0, places=9)

    def test_a_negative_reading_is_clamped_not_subtracted(self):
        # #768's own manual reconstruction found "a real -17.48 kWh of
        # negative-signed readings" on a real load's power sensor, with no
        # mechanism that could explain them, and excluded them. A
        # Controllable Load is a consumer; letting a negative reading
        # reduce delivered energy would report it as having generated.
        by_period, total = clh.integrate_delivered_kwh([3.0, -3.0, 3.0], 1.0, set())
        self.assertEqual(by_period, (3.0, 0.0, 3.0))
        self.assertAlmostEqual(total, 6.0, places=9)


class TestTheNumberIsReal(_NativeModeCase):
    def test_two_loads_each_reconstruct_their_own_delivered_energy(self):
        solver_writer.NATIVE.hass = _native_hass(
            [_subentry("s_hws", _HWS), _subentry("s_pool", _POOL)]
        )
        # HWS: 0.65 kW for hours 2,3,4 -> 1.95 kWh.
        hws = [0.0] * 24
        hws[2] = hws[3] = hws[4] = 0.65
        # Pool: 1.2 kW for hours 10..13 -> 4.8 kWh.
        pool = [0.0] * 24
        for h in (10, 11, 12, 13):
            pool[h] = 1.2
        with _power_history(
            {
                "sensor.wwk302_power": _hourly(hws),
                "sensor.pool_pump_power": _hourly(pool),
            }
        ):
            out = {d.subentry_id: d for d in _resolve()}

        self.assertEqual(set(out), {"s_hws", "s_pool"})
        self.assertTrue(out["s_hws"].scorable)
        self.assertEqual(out["s_hws"].reason, clh.REASON_OK)
        self.assertAlmostEqual(out["s_hws"].delivered_kwh, 1.95, places=6)
        self.assertAlmostEqual(out["s_pool"].delivered_kwh, 4.8, places=6)
        # The per-period series is what the oracle will be handed; the
        # total is only its sum, so the placement has to be right too.
        self.assertEqual(
            [i for i, v in enumerate(out["s_hws"].delivered_kwh_by_period) if v > 0],
            [2, 3, 4],
        )
        self.assertEqual(out["s_hws"].kind, "deferrable")
        self.assertEqual(out["s_pool"].kind, "sheddable")
        self.assertEqual(out["s_hws"].observed_period_fraction, 1.0)

    def test_the_configured_target_is_never_what_is_reported(self):
        """Mark's own decided convention, 2026-09-27: real-delivered kWh,
        not the configured target. The fixture's target (4.0 kWh) and its
        real delivery (1.95 kWh) differ on purpose, so a regression that
        read the config field would be visible rather than plausible."""
        solver_writer.NATIVE.hass = _native_hass([_subentry("s_hws", _HWS)])
        hws = [0.0] * 24
        hws[2] = hws[3] = hws[4] = 0.65
        with _power_history({"sensor.wwk302_power": _hourly(hws)}):
            (out,) = _resolve()
        self.assertNotAlmostEqual(out.delivered_kwh, _HWS["deferrable_target_kwh"])
        self.assertAlmostEqual(out.delivered_kwh, 1.95, places=6)

    def test_a_battery_participant_subentry_is_not_a_controllable_load(self):
        solver_writer.NATIVE.hass = _native_hass(
            [
                _subentry(
                    "s_ev", {"battery_participant_name": "ev"}, "battery_participant"
                )
            ]
        )
        with _power_history({}):
            self.assertEqual(_resolve(), [])


class TestARecorderGapCreditsNothing(_NativeModeCase):
    """#1161's guard, reached through this reconstruction rather than
    restated inside it."""

    def test_it_reuses_the_repositorys_own_constant_rather_than_a_literal(self):
        self.assertEqual(load_run_state.MAX_SAMPLE_GAP_HOURS, 1.0)
        solver_writer.NATIVE.hass = _native_hass([_subentry("s_pool", _POOL)])
        # One sample only, at 00:00, at 1.2 kW. resample_history_mean()
        # holds it forward across all 24 hours, so an unguarded integration
        # credits 28.8 kWh for a pump nobody observed running.
        rows = [(DAY_START, 1.2)]
        with _power_history({"sensor.pool_pump_power": rows}):
            (out,) = _resolve()
        self.assertTrue(out.scorable)
        self.assertLess(out.delivered_kwh, 28.8)
        # Periods 0 and 1 start within one hour of the single sample (the
        # guard is "gap greater than MAX_SAMPLE_GAP_HOURS", so an exactly
        # one-hour-old reading still speaks); period 2 onward do not.
        self.assertAlmostEqual(out.delivered_kwh, 2.4, places=6)
        self.assertEqual(out.stale_period_count, 22)
        self.assertAlmostEqual(out.observed_period_fraction, round(2 / 24, 4), places=4)

    def test_widening_the_guard_moves_the_answer(self):
        """Proof the number really comes from that helper: the same fixture,
        with the guard widened at its source, credits more hours. A
        hardcoded 1.0 inside this module could not respond to this."""
        solver_writer.NATIVE.hass = _native_hass([_subentry("s_pool", _POOL)])
        rows = [(DAY_START, 1.2)]
        real = solver_writer._stale_power_period_indices

        def _wide(points, grid_times, period_hours, **kwargs):
            return real(points, grid_times, period_hours, max_gap_hours=4.0)

        with (
            _power_history({"sensor.pool_pump_power": rows}),
            patch.object(solver_writer, "_stale_power_period_indices", _wide),
        ):
            (out,) = _resolve()
        # Periods 0..4 are now within four hours of the sample: 5 x 1.2 kWh.
        self.assertAlmostEqual(out.delivered_kwh, 6.0, places=6)
        self.assertEqual(out.stale_period_count, 19)


class TestAbsenceIsAbsence(_NativeModeCase):
    def test_no_power_sensor_reports_a_reason_and_not_zero(self):
        data = dict(_POOL)
        del data["controllable_load_power_sensor"]
        solver_writer.NATIVE.hass = _native_hass([_subentry("s_pool", data)])
        # Discovery is what would otherwise fill the gap; here it finds
        # nothing, which is the real case for a switch with no metering.
        with (
            _power_history({}),
            patch.object(
                solver_writer,
                "resolve_controllable_load_power_sensor",
                return_value=None,
            ),
        ):
            (out,) = _resolve()
        self.assertFalse(out.scorable)
        self.assertEqual(out.reason, clh.REASON_NO_POWER_SENSOR)
        self.assertIsNone(out.delivered_kwh)
        self.assertIsNone(out.observed_period_fraction)
        self.assertEqual(out.delivered_kwh_by_period, ())

    def test_a_day_before_the_sensor_existed_is_not_scorable(self):
        solver_writer.NATIVE.hass = _native_hass([_subentry("s_hws", _HWS)])
        with _power_history({"sensor.wwk302_power": []}):
            (out,) = _resolve()
        self.assertFalse(out.scorable)
        self.assertEqual(out.reason, clh.REASON_NO_HISTORY)
        self.assertIsNone(out.delivered_kwh)
        # The load still appears: a household has to be able to see which
        # of its loads the day's score could not read.
        self.assertEqual(out.name, "Hot water")
        self.assertEqual(out.power_sensor, "sensor.wwk302_power")

    def test_the_published_summary_carries_null_rather_than_a_number(self):
        solver_writer.NATIVE.hass = _native_hass([_subentry("s_hws", _HWS)])
        with _power_history({"sensor.wwk302_power": []}):
            (out,) = _resolve()
        attr = out.as_attribute()
        self.assertIsNone(attr["delivered_kwh"])
        self.assertEqual(attr["reason"], clh.REASON_NO_HISTORY)
        self.assertFalse(attr["scorable"])
        # Compact by design: the per-period series is not published, or one
        # field would carry 288 floats per load past the recorder's own
        # 16 KB attribute cap (#944).
        self.assertNotIn("delivered_kwh_by_period", attr)


class TestStandaloneModeIsAnEmptyNoOp(_NativeModeCase):
    def test_no_hass_means_no_reconstruction_and_no_error(self):
        solver_writer.NATIVE.hass = None
        self.assertEqual(_resolve(), [])


class TestDiscoveryFillsTheGapWhenNothingIsConfigured(_NativeModeCase):
    """#768's own earlier prerequisite (v0.94.292) resolves a load's power
    sensor off its device. This reconstruction must go through that
    resolver, not read the config field directly, or every load relying on
    discovery would silently report as not scorable."""

    def test_a_discovered_sensor_is_used(self):
        data = dict(_HWS)
        del data["controllable_load_power_sensor"]
        solver_writer.NATIVE.hass = _native_hass([_subentry("s_hws", data)])
        hws = [0.0] * 24
        hws[5] = 0.65
        with (
            _power_history({"sensor.wwk302_power": _hourly(hws)}),
            patch.object(
                solver_writer,
                "resolve_controllable_load_power_sensor",
                return_value="sensor.wwk302_power",
            ),
        ):
            (out,) = _resolve()
        self.assertTrue(out.scorable)
        self.assertEqual(out.power_sensor, "sensor.wwk302_power")
        self.assertAlmostEqual(out.delivered_kwh, 0.65, places=6)


if __name__ == "__main__":
    unittest.main()
