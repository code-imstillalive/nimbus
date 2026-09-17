"""nimbus #1098 (Mark Purcell): a battery participant that was away for
part of the window cannot have its energy balance read as an efficiency.

`_resolve_battery_participant_history()` deliberately ZEROES a
participant's charge/discharge for every period it is away (#768/#467),
and correctly so -- a car's propulsion discharge, or a charge it took
somewhere else, never touches this household's grid connection and must
not be priced as though it did.

But `initial_soc_kwh`/`final_soc_kwh` come from the participant's own SoC
telemetry, which reports **every** real state change, home or away. So on
an away-heavy day the two sides of the balance are answering different
questions -- grid-relevant flow versus total real pack state -- and were
never going to reconcile.

**Mark's real data, 17 Sep, his own install.** `ev_my` left home three
times (07:53-08:37, 12:33-13:30, 18:20-21:35), ~4h56m total:

    modelled_soc_delta_kwh        25.498
    measured_soc_delta_kwh        37.157
    residual_kwh                 -11.659     (19.4% of its capacity)
    implied_charge_efficiency      1.2849    <- 128.5%, impossible
    implied_efficiency_reason    "implied_efficiency_above_unity"

That reason is the label for a genuine sign or reference-plane error.
This is not one, and a household reading it has no way to tell "this
pack's efficiency model is broken" from "this car went out for the day"
-- which is the common case on any install with an EV participant, not
an edge case.

He closed off the obvious alternative before filing:
`battery_participant_power_sensor` on both his EVs is already the
comprehensive pack-level reading (driving + AC + DC charging), so there
is no better sensor to configure and this cannot be fixed by setup.

## Why this reason is checked FIRST, ahead of every other

`TestItPreemptsSocMovedWithoutThroughput` is the load-bearing one, and it
guards something Mark's filing implies but does not say outright.

A participant away for most of a window has its throughput masked to
~zero while its SoC moves freely. That is precisely
`soc_moved_without_throughput`'s trigger -- and that branch asserts the
participant's power sensor "failed to see its dispatch", a reconstruction
blind spot. Here the sensor saw it perfectly well and **Nimbus masked it
on purpose**. Firing that would accuse the household's hardware of a
fault this code introduced deliberately.

## On the threshold

1% of the window, mirroring `_MIXED_WINDOW_MIN_FRACTION` -- but for a
different reason, and the difference is why the number is low rather than
a comfortable-looking 5%.

For #1072/#1073 the 1% floor is a *negligibility* threshold: a hundredth
of the throughput genuinely cannot move the reading much. Away time has
no such property. **Unaccounted energy is not proportional to time
away** -- a fifteen-minute DC fast-charge stop is well under 1% of a day
and can put 30 kWh into a pack, none of it visible to the household's
grid connection.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer

# Mark's own 17 Sep figures for ev_my, as filed.
REAL_AWAY_SECONDS = (
    (8 * 60 + 37 - 7 * 60 - 53) * 60
    + (13 * 60 + 30 - 12 * 60 - 33) * 60
    + (21 * 60 + 35 - 18 * 60 - 20) * 60
)


def _balance(**overrides):
    kwargs = {
        "name": "ev_my",
        "in_kwh": 20.0,
        "out_kwh": 5.0,
        "initial_soc_kwh": 30.0,
        "final_soc_kwh": 67.157,
        "capacity_kwh": 60.0,
        "charge_efficiency": 0.9747,
        "discharge_efficiency": 0.9747,
        "away_period_count": 0,
        "n_periods": 48,
    }
    kwargs.update(overrides)
    return solver_writer.battery_energy_balance(**kwargs)


class TestTheRealDay(unittest.TestCase):
    """~4h56m of a 24h window is 20.6%, which on a 48-period grid is
    ~10 periods."""

    def setUp(self):
        self.result = _balance(away_period_count=10, n_periods=48)

    def test_it_reports_the_away_reason_not_an_efficiency_fault(self):
        self.assertEqual(
            self.result["implied_efficiency_reason"],
            "participant_away_during_window",
            "an away-heavy participant is still being reported as an "
            "efficiency finding -- a household cannot tell that apart "
            "from a genuinely broken pack",
        )

    def test_the_away_fraction_is_published(self):
        """The reason says the comparison is void; this says by how far.
        Without it a reader has to go back to the availability sensor's
        own history to judge whether the number is merely caveated or
        entirely meaningless."""
        self.assertAlmostEqual(
            self.result["participant_away_fraction"], 0.2083, places=3
        )

    def test_the_numbers_are_kept_rather_than_nulled(self):
        """Same choice #1073 made for the mixed-window case: the figures
        still bound the answer and a reader who understands the caveat
        can use them. What changes is that the caveat is attached."""
        self.assertIsNotNone(self.result["implied_charge_efficiency"])
        self.assertIsNotNone(self.result["residual_kwh"])

    def test_marks_filed_away_duration_is_what_this_file_claims(self):
        """Guards the fixture's own premise. 4h56m against a 24h window
        is 20.6%, which is what `away_period_count=10, n_periods=48`
        represents -- if that arithmetic is wrong the real-data framing
        above is wrong too."""
        self.assertAlmostEqual(REAL_AWAY_SECONDS / 3600.0, 4.933, places=2)
        self.assertAlmostEqual(REAL_AWAY_SECONDS / 86400.0, 0.2056, places=3)


class TestItPreemptsSocMovedWithoutThroughput(unittest.TestCase):
    """The reason precedence matters, and this is why.

    A participant away for most of the window has masked (zero)
    throughput and a freely-moving SoC -- exactly
    `soc_moved_without_throughput`'s trigger, which asserts the power
    sensor failed to see real dispatch. Here the sensor saw it and Nimbus
    masked it deliberately.
    """

    def test_away_all_day_with_masked_power_is_not_a_sensor_blind_spot(self):
        result = _balance(
            name="ev_m3p",
            in_kwh=0.0,
            out_kwh=0.0,
            initial_soc_kwh=50.0,
            final_soc_kwh=43.904,
            away_period_count=40,
            n_periods=48,
        )
        self.assertEqual(
            result["implied_efficiency_reason"],
            "participant_away_during_window",
            "a car that was away all day is being reported as its own "
            "power sensor failing -- the sensor is fine, this code masked "
            "the readings on purpose (#768/#467)",
        )

    def test_the_same_shape_at_home_is_still_a_sensor_blind_spot(self):
        """#1012's finding must survive: with no away time, zero
        throughput against a real SoC move is still a genuine
        reconstruction gap and must keep its own name."""
        result = _balance(
            name="home",
            in_kwh=0.0,
            out_kwh=0.0,
            initial_soc_kwh=50.0,
            final_soc_kwh=43.904,
            away_period_count=0,
            n_periods=48,
        )
        self.assertEqual(
            result["implied_efficiency_reason"], "soc_moved_without_throughput"
        )


class TestTheThreshold(unittest.TestCase):
    def test_one_percent_is_flagged(self):
        self.assertEqual(
            _balance(away_period_count=2, n_periods=200)["implied_efficiency_reason"],
            "participant_away_during_window",
        )

    def test_below_one_percent_falls_through_to_the_ordinary_reason(self):
        result = _balance(away_period_count=1, n_periods=200)
        self.assertNotEqual(
            result["implied_efficiency_reason"], "participant_away_during_window"
        )

    def test_it_mirrors_the_mixed_window_floor(self):
        """Kept identical to #1072/#1073's floor so there is one notion of
        'negligible' in this file. If someone raises this to a
        comfortable-looking 5%, they should have to change it knowingly
        -- a 15-minute DC fast charge is under 1% of a day and can hide
        30 kWh."""
        self.assertEqual(
            solver_writer._AWAY_WINDOW_MIN_FRACTION,
            solver_writer._MIXED_WINDOW_MIN_FRACTION,
        )


class TestTheHomeBatteryIsUnaffected(unittest.TestCase):
    """A home battery has no availability entity and carries
    `unavailable_period_indices=None`, which the call site turns into 0.
    Every existing reason must behave exactly as before for it.
    """

    def test_a_normal_home_day_keeps_its_existing_reason(self):
        result = _balance(name="home", away_period_count=0, n_periods=48)
        self.assertNotEqual(
            result["implied_efficiency_reason"], "participant_away_during_window"
        )
        self.assertEqual(result["participant_away_fraction"], 0.0)

    def test_the_field_is_always_present_even_at_zero(self):
        """Published unconditionally so a consumer never has to
        distinguish missing from zero."""
        self.assertIn("participant_away_fraction", _balance())

    def test_defaults_reproduce_the_pre_1098_behaviour(self):
        """Both new parameters default to 0, so every existing caller and
        test -- including the standalone/cron writer, which builds no
        participants -- is untouched."""
        explicit = _balance(away_period_count=0, n_periods=0)
        self.assertEqual(
            explicit["implied_efficiency_reason"],
            solver_writer.battery_energy_balance(
                name="ev_my",
                in_kwh=20.0,
                out_kwh=5.0,
                initial_soc_kwh=30.0,
                final_soc_kwh=67.157,
                capacity_kwh=60.0,
                charge_efficiency=0.9747,
                discharge_efficiency=0.9747,
            )["implied_efficiency_reason"],
        )

    def test_a_zero_period_window_does_not_divide_by_zero(self):
        self.assertEqual(
            _balance(away_period_count=5, n_periods=0)["participant_away_fraction"], 0.0
        )


class TestTheCallSiteReadsTheMaskItAlreadyHas(unittest.TestCase):
    """Mark suggested threading an away fraction down from
    `_resolve_battery_participant_history()`. It turned out not to be
    needed: that function already puts the same mask on the participant's
    own `BatteryConfig.unavailable_period_indices` (#467), so the call
    site can read it off the config it is already handed.

    Source-checked because the wiring is what regresses -- driving it
    needs a real participant with real availability history.
    """

    def test_the_call_site_passes_the_configs_own_mask(self):
        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = " ".join(f.read().split())
        self.assertIn("away_period_count=len(b.unavailable_period_indices or ())", text)
        self.assertIn("n_periods=len(period_hours_arr)", text)


if __name__ == "__main__":
    unittest.main()
