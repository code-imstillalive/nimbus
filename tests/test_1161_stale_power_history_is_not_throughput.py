"""nimbus #1161: a participant's achieved reconstruction must not credit
throughput across a recorder gap it never observed.

`resample_history_mean()` falls back to nearest-at-or-before for any
period with no samples of its own, and documents that as deliberate --
*"never fabricates a gap."* For a STATE (SoC, price) that is the
physically correct model. For POWER it is not sample-and-hold at all: it
integrates a stale instantaneous reading across time nobody observed.

Measured on this repo's own resample before any of this was written, 24
one-hour periods with samples present only 00:00-02:00 at 5.0 kW:

    hours 0-4    [5.0, 5.0, 5.0, 5.0, 5.0]
    hours 20-23  [5.0, 5.0, 5.0, 5.0]
    nonzero periods: 24 / 24

So a participant whose pack sensor stopped writing rows after an
early-morning discharge was credited 5 kW x 21 h = **105 kWh** that never
flowed -- 175% of a 60 kWh pack -- and every kWh of it was then priced
against real tariffs by `evaluate_realized_cost_multi()`.

## Why this is a guard the repo already owns

`load_run_state.py` has refused exactly this since it was written::

    if 0.0 < dt_hours <= MAX_SAMPLE_GAP_HOURS:
        delivered += power_kw * dt_hours

One hour is this repo's existing answer to "how long may a power reading
speak for". The retrospective scorer simply never inherited it, so the
live counter and the scorer disagreed about the same question. These
tests pin that they now agree, by reading the constant rather than
restating it -- retyping `1.0` here would let the two drift apart again
silently, which is the whole failure being fixed.

## What is deliberately NOT asserted

That an EV participant hits this on any particular real day. The
mechanism is what is provable from the code; the frequency depends on an
install's own telemetry. `_resolve_battery_participant_history()` is
driven for real below rather than the helper being tested alone, because
a helper that is correct and unwired is the shape this issue's own
sibling defects keep taking.
"""

from __future__ import annotations

import contextlib
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import load_run_state
import numpy as np
import solver_writer
from solver_inputs import battery_participants as battery_participants_inputs

BRISBANE = solver_writer.LOCAL_TZ
DAY_START = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)
GRID = [DAY_START + timedelta(hours=i) for i in range(24)]

_EV_DATA = {
    "battery_participant_name": "ev_m3p",
    "battery_participant_power_sensor": "sensor.m3p_pack_power",
    "battery_participant_soc_sensor": "sensor.m3p_battery_level",
    "battery_participant_capacity_kwh": 60.0,
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 11.0,
    "battery_participant_min_soc_percent": 10.0,
    "battery_participant_max_soc_percent": 100.0,
}


def _fake_subentry(sid, stype, data):
    return SimpleNamespace(subentry_id=sid, subentry_type=stype, data=data)


def _fake_state(value):
    return SimpleNamespace(state=value, attributes={})


def _fake_native_hass(subentries, states=None):
    states = states or {}
    entry = SimpleNamespace(domain="nimbus_load", subentries={})
    for se in subentries:
        entry.subentries[se.subentry_id] = se
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _domain: [entry]),
        states=SimpleNamespace(get=lambda eid: states.get(eid)),
    )


@contextlib.contextmanager
def _patch_history(power_rows, soc_rows):
    """Back the two reads this function makes with separate fixtures --
    the SoC series must stay fully covered while the power series has the
    gap, because that asymmetry is the situation being tested."""
    with (
        patch.object(
            solver_writer, "fetch_entity_power_history_kw", return_value=power_rows
        ),
        patch.object(
            solver_writer, "fetch_entity_history_range", return_value=soc_rows
        ),
    ):
        yield


def _soc_series():
    return [(DAY_START + timedelta(hours=i), 55.0 - i * 0.5) for i in range(25)]


def _call():
    return battery_participants_inputs._resolve_battery_participant_history(
        day_start=DAY_START,
        day_end=DAY_END,
        grid_times=GRID,
        period_hours=1.0,
        n_periods=24,
    )


class TestTheMeasuredResampleBehaviour(unittest.TestCase):
    """Pins the premise this whole file rests on. If `resample_history_mean()`
    ever stops holding the last reading forward, the fix below becomes a
    no-op and these tests would keep passing while guarding nothing."""

    def test_the_resample_really_does_hold_a_stale_reading_all_day(self):
        sparse = [(DAY_START + timedelta(minutes=m), 5.0) for m in (0, 30, 60, 90, 120)]
        out = solver_writer.resample_history_mean(sparse, GRID, 1.0)
        self.assertEqual(
            [round(v, 3) for v in out[20:]],
            [5.0, 5.0, 5.0, 5.0],
            "the premise of #1161 no longer holds -- resample_history_mean() "
            "has stopped carrying the last sample forward, so the guard "
            "below is now protecting against nothing",
        )
        self.assertEqual(sum(1 for v in out if v != 0.0), 24)


class TestTheGuardUsesTheRepositorysOwnConstant(unittest.TestCase):
    def test_it_reads_load_run_state_rather_than_restating_the_number(self):
        """Retyping 1.0 here is how the live counter and the scorer would
        drift apart again -- the exact disagreement this issue is."""
        self.assertEqual(load_run_state.MAX_SAMPLE_GAP_HOURS, 1.0)
        pts = [(DAY_START, 5.0)]
        # Period 2 starts 2h after the only sample: stale under a 1.0h
        # guard, trustworthy under a 3.0h one. If the helper had hardcoded
        # its own number, the override below could not move it.
        default_stale = solver_writer._stale_power_period_indices(pts, GRID, 1.0)
        widened = solver_writer._stale_power_period_indices(
            pts, GRID, 1.0, max_gap_hours=3.0
        )
        self.assertIn(2, default_stale)
        self.assertNotIn(2, widened)


class TestStalePeriodsContributeNoThroughput(unittest.TestCase):
    """The defect itself, driven through the real reconstruction."""

    def setUp(self):
        self._orig = solver_writer._NATIVE_HASS
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", _EV_DATA)],
            states={"sensor.m3p_battery_level": _fake_state("55.0")},
        )

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig

    def test_a_sensor_that_stops_recording_credits_only_what_it_saw(self):
        # 5 kW discharge recorded 00:00-02:00, then the sensor goes quiet
        # for the rest of the day.
        power = [(DAY_START + timedelta(minutes=m), 5.0) for m in (0, 30, 60, 90, 120)]
        with _patch_history(power, _soc_series()):
            resolved = _call()

        self.assertEqual(len(resolved), 1, "the participant should still be scored")
        _cfg, charge_kw, discharge_kw, _fin, _sh = resolved[0]

        # period_hours == 1.0, so kW summed IS kWh.
        credited = float(np.sum(charge_kw) + np.sum(discharge_kw))
        self.assertLessEqual(
            credited,
            20.0,
            f"credited {credited:.1f} kWh of throughput from a sensor that "
            "only recorded two hours of it -- the stale-hold defect is back",
        )
        self.assertGreater(
            credited, 0.0, "the hours that WERE recorded must still count"
        )

    def test_the_unrecorded_periods_are_exactly_zero(self):
        power = [(DAY_START + timedelta(minutes=m), 5.0) for m in (0, 30, 60, 90, 120)]
        with _patch_history(power, _soc_series()):
            _cfg, charge_kw, discharge_kw, _fin, _sh = _call()[0]
        tail = [
            float(charge_kw[i] + discharge_kw[i]) for i in range(4, 24)
        ]  # well past the last sample
        self.assertEqual(
            tail,
            [0.0] * 20,
            "periods with no recorded power must contribute nothing, not the "
            "last reading held forward",
        )

    def test_a_fully_recorded_day_is_untouched(self):
        """The guard must not cost anything on an install whose sensor
        reports normally -- otherwise it is a regression dressed as a fix."""
        power = [(DAY_START + timedelta(minutes=15 * i), 4.0) for i in range(96)]
        with _patch_history(power, _soc_series()):
            _cfg, charge_kw, discharge_kw, _fin, _sh = _call()[0]
        credited = float(np.sum(charge_kw) + np.sum(discharge_kw))
        self.assertAlmostEqual(
            credited,
            96.0,
            places=3,
            msg="a continuously-recorded day lost throughput to the gap guard",
        )

    def test_a_brief_hold_is_still_trusted(self):
        """A sensor that writes every 45 minutes is normal recorder
        behaviour, not a gap. Zeroing those periods would make the guard
        fire on ordinary installs."""
        power = [(DAY_START + timedelta(minutes=45 * i), 4.0) for i in range(32)]
        with _patch_history(power, _soc_series()):
            _cfg, charge_kw, discharge_kw, _fin, _sh = _call()[0]
        credited = float(np.sum(charge_kw) + np.sum(discharge_kw))
        self.assertAlmostEqual(credited, 96.0, places=3)


class TestTheCaveatIsPublishedRatherThanSilent(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer._NATIVE_HASS
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", _EV_DATA)],
            states={"sensor.m3p_battery_level": _fake_state("55.0")},
        )

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig

    def test_the_config_carries_the_unobserved_periods(self):
        power = [(DAY_START + timedelta(minutes=m), 5.0) for m in (0, 30, 60, 90, 120)]
        with _patch_history(power, _soc_series()):
            cfg, _c, _d, _f, _sh = _call()[0]
        self.assertIsNotNone(
            cfg.stale_history_period_indices,
            "silently zeroing a fifth of the day's throughput without saying "
            "so is the absence-as-signal failure this repo keeps recording",
        )
        self.assertGreater(len(cfg.stale_history_period_indices), 15)

    def test_a_clean_day_carries_none_rather_than_an_empty_set(self):
        power = [(DAY_START + timedelta(minutes=15 * i), 4.0) for i in range(96)]
        with _patch_history(power, _soc_series()):
            cfg, _c, _d, _f, _sh = _call()[0]
        self.assertIsNone(cfg.stale_history_period_indices)

    def test_staleness_is_not_folded_into_the_availability_gate(self):
        """The distinction the separate field exists for. `unavailable_
        period_indices` is an LP gate -- telling the oracle a car was away
        whenever its sensor went quiet would be #467's own bug pointed the
        other way."""
        power = [(DAY_START + timedelta(minutes=m), 5.0) for m in (0, 30, 60, 90, 120)]
        with _patch_history(power, _soc_series()):
            cfg, _c, _d, _f, _sh = _call()[0]
        self.assertIsNone(
            cfg.unavailable_period_indices,
            "an unrecorded period was reported to the oracle as the car being "
            "away -- those are different claims about the world",
        )

    def test_the_balance_publishes_the_unobserved_fraction(self):
        result = solver_writer.battery_energy_balance(
            name="ev_m3p",
            in_kwh=10.0,
            out_kwh=0.0,
            initial_soc_kwh=30.0,
            final_soc_kwh=39.0,
            capacity_kwh=60.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            stale_period_count=6,
            n_periods=24,
        )
        self.assertAlmostEqual(result["unobserved_power_fraction"], 0.25, places=4)

    def test_the_fraction_is_always_present(self):
        """0.0 rather than missing, so a consumer never has to tell absent
        from zero -- the same choice #1098 made for the away fraction."""
        result = solver_writer.battery_energy_balance(
            name="home",
            in_kwh=10.0,
            out_kwh=0.0,
            initial_soc_kwh=30.0,
            final_soc_kwh=39.0,
            capacity_kwh=60.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            n_periods=24,
        )
        self.assertEqual(result["unobserved_power_fraction"], 0.0)

    def test_it_is_reported_separately_from_the_away_fraction(self):
        """Away explains a residual; unobserved says the residual cannot
        be explained from this data. Collapsing them would lose that."""
        result = solver_writer.battery_energy_balance(
            name="ev_m3p",
            in_kwh=10.0,
            out_kwh=0.0,
            initial_soc_kwh=30.0,
            final_soc_kwh=39.0,
            capacity_kwh=60.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            away_period_count=3,
            stale_period_count=6,
            n_periods=24,
        )
        self.assertAlmostEqual(result["participant_away_fraction"], 0.125, places=4)
        self.assertAlmostEqual(result["unobserved_power_fraction"], 0.25, places=4)


class TestTheHelperItself(unittest.TestCase):
    def test_no_grid_is_a_real_no_op(self):
        self.assertEqual(
            solver_writer._stale_power_period_indices([], [], 1.0), frozenset()
        )

    def test_no_samples_at_all_is_every_period(self):
        """The participant path never reaches this (an empty history
        excludes the participant outright), but 'none stale' would be the
        wrong answer for any future caller."""
        self.assertEqual(
            solver_writer._stale_power_period_indices([], GRID, 1.0),
            frozenset(range(24)),
        )

    def test_periods_before_the_first_sample_count_as_unobserved(self):
        pts = [(DAY_START + timedelta(hours=12), 7.0)]
        stale = solver_writer._stale_power_period_indices(pts, GRID, 1.0)
        self.assertTrue(set(range(12)).issubset(stale))
        self.assertNotIn(12, stale)

    def test_unsorted_input_is_handled(self):
        pts = [(DAY_START + timedelta(hours=3), 1.0), (DAY_START, 2.0)]
        stale = solver_writer._stale_power_period_indices(pts, GRID, 1.0)
        self.assertNotIn(0, stale)
        self.assertNotIn(3, stale)


if __name__ == "__main__":
    unittest.main()
