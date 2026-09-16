"""nimbus issue #949 -- `j_ach_hourly`'s `soc_pct` is fleet-blended, while
the `soc_discrepancy_*` diagnostic compares it against **one** sensor.

`solver_writer.py`'s discrepancy check reads `cfg["solver_battery_soc_
sensor"]` -- the home battery's own SoC -- and compares it hour by hour
against `j_ach_hourly[...]["soc_pct"]`, which `quality_report.py` builds
as `total stored kWh / total capacity kWh` across **every** scored
battery. `quality_report.py` says so in its own comment. On a
single-battery install the two are the same quantity; the moment a
`battery_participant` is scored they are not, and the diagnostic reports
the difference as if it were sensor disagreement.

These tests pin the arithmetic that produces it. They are deliberately
**characterisation tests, not a fix** -- #949 offers three fix options,
all of which change a published diagnostic on live installs, and the
issue's own position is that choosing is a decision rather than a patch.
What has been missing is the defect being executable instead of argued,
so a chosen fix can be checked against something.

**Scope, stated because this issue has already been over-claimed once.**
The fleet blend is real, and it is NOT the whole story. #949's third
comment reported 30.46 pt on a genuine single-battery install, where
blending contributes exactly nothing by construction -- so something
else was also wrong. That something was found: #1008, the
sample-averaged (rather than time-weighted) `resample_history_mean()`,
which drove the achieved trajectory to a fictional 0.4429% endpoint on
the day in question. The 30.46 pt figure is `30.9 - 0.44` to the
decimal, so it is built on that fictional endpoint and does not survive
#1008's fix. These tests therefore cover only the part that is
independently true: the blend itself.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.quality_report import compute_quality_report

N = 24
HOURS = np.ones(N)
START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)

# Deliberately the shape #949 tabulates: a modest home pack against EV
# packs large enough that the blend is dominated by the cars. The issue's
# own worked example is a 30 kWh home battery against two 75 kWh EVs, at
# which point the EVs are 83% of total capacity.
HOME_CAPACITY = 30.0
EV_CAPACITY = 75.0


def _battery(name: str, capacity_kwh: float, initial_soc_kwh: float) -> BatteryConfig:
    return BatteryConfig(
        name=name,
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=capacity_kwh * 0.02,
        max_soc_kwh=capacity_kwh,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


def _idle_report(batteries: list[BatteryConfig]):
    """Score a day on which every battery sits perfectly still.

    Idle is the point. With no charge and no discharge anywhere, the
    achieved SoC trajectory is a flat line at each pack's own starting
    SoC, so any movement in the published `soc_pct` is the blend and
    nothing else -- no efficiency, no integration drift, no sensor gap.
    """
    zero = np.zeros(N)
    grid = GridConfig(
        import_price=np.full(N, 0.30),
        export_price=np.full(N, 0.05),
        import_limit_kw=42.0,
        export_limit_kw=42.0,
    )
    return compute_quality_report(
        periods=PeriodGrid(hours=HOURS, start=START),
        grid_residual=grid,
        grid_oracle=grid,
        batteries=batteries,
        solar=SolarConfig(forecast_kw=np.zeros(N)),
        load=LoadConfig(name="house", forecast_kw=np.full(N, 1.0)),
        timestamps=[START + timedelta(hours=i) for i in range(N)],
        real_p2p_dollars_earned=0.0,
        commanded_charge_kw=[zero for _ in batteries],
        commanded_discharge_kw=[zero for _ in batteries],
        actual_charge_kw=[zero for _ in batteries],
        actual_discharge_kw=[zero for _ in batteries],
        final_soc_kwh_actual=[b.initial_soc_kwh for b in batteries],
    )


def _first_soc_pct(report) -> float:
    """The published hourly `soc_pct`, as a consumer would read it.

    Deliberately the PUBLISHED value rather than the internal array:
    `_hourly_means_by_key()` rounds to 4 dp on the way out, so exact
    comparisons here must be to 4 dp too. Asserting against unrounded
    arithmetic passes by luck on round numbers and fails on 71.428571,
    which is how this was found.
    """
    rows = report.j_ach_hourly
    key = min(rows)
    return float(rows[key]["soc_pct"])


class TestTheBlendIsInertOnASingleBattery(unittest.TestCase):
    """The control case. If this ever fails, the tests below are
    measuring something other than blending."""

    def test_one_battery_publishes_its_own_soc(self):
        home = _battery("home", HOME_CAPACITY, HOME_CAPACITY * 0.50)
        self.assertAlmostEqual(_first_soc_pct(_idle_report([home])), 50.0, places=6)

    def test_it_tracks_whatever_that_one_battery_holds(self):
        for pct in (2.0, 17.4, 50.0, 88.0, 100.0):
            with self.subTest(pct=pct):
                home = _battery("home", HOME_CAPACITY, HOME_CAPACITY * pct / 100.0)
                self.assertAlmostEqual(
                    _first_soc_pct(_idle_report([home])), pct, places=6
                )


class TestAParticipantMovesThePublishedSoCAwayFromTheHomePack(unittest.TestCase):
    """The defect, as arithmetic. The home battery has not moved and its
    own sensor would read 50.0% in every case below; the published
    number does not."""

    def test_one_ev_at_eighty_percent_pulls_the_blend_up(self):
        home = _battery("home", HOME_CAPACITY, HOME_CAPACITY * 0.50)
        ev = _battery("ev_m3p", EV_CAPACITY, EV_CAPACITY * 0.80)

        published = _first_soc_pct(_idle_report([home, ev]))
        expected = round(
            (HOME_CAPACITY * 0.50 + EV_CAPACITY * 0.80)
            / (HOME_CAPACITY + EV_CAPACITY)
            * 100.0,
            4,
        )

        self.assertAlmostEqual(published, expected, places=6)
        self.assertAlmostEqual(published, 71.4286, places=4)
        self.assertAlmostEqual(
            abs(published - 50.0),
            21.4286,
            places=4,
            msg="21.4 points of apparent SoC disagreement, from a day on "
            "which nothing moved and no sensor is wrong -- this is what "
            "soc_discrepancy_max_pct would report (nimbus #949)",
        )

    def test_two_evs_reproduce_the_issues_own_worked_example(self):
        """#949's table: home + 2 EVs -> 25.0 pt."""
        home = _battery("home", HOME_CAPACITY, HOME_CAPACITY * 0.50)
        evs = [
            _battery("ev_m3p", EV_CAPACITY, EV_CAPACITY * 0.80),
            _battery("ev_my", EV_CAPACITY, EV_CAPACITY * 0.80),
        ]
        published = _first_soc_pct(_idle_report([home, *evs]))
        self.assertAlmostEqual(published, 75.0, places=4)
        self.assertAlmostEqual(abs(published - 50.0), 25.0, places=4)

    def test_near_empty_evs_pull_it_the_other_way(self):
        """The error is not a fixed offset, and it is not signed. Cars
        that arrive home empty drag the blend BELOW the home pack --
        #949's fourth row, 29.2 pt in the opposite direction."""
        home = _battery("home", HOME_CAPACITY, HOME_CAPACITY * 0.50)
        evs = [
            _battery("ev_m3p", EV_CAPACITY, EV_CAPACITY * 0.15),
            _battery("ev_my", EV_CAPACITY, EV_CAPACITY * 0.15),
        ]
        published = _first_soc_pct(_idle_report([home, *evs]))
        self.assertLess(published, 50.0)
        self.assertAlmostEqual(published, 20.8333, places=4)
        self.assertAlmostEqual(abs(published - 50.0), 29.1667, places=4)

    def test_the_magnitude_follows_capacity_share_not_battery_count(self):
        """Worth pinning because 'more batteries = worse' is the natural
        reading and it is wrong. A tiny participant barely moves the
        blend however many of them there are; one large one moves it a
        long way. What matters is the share of total capacity that is
        not the battery being compared against."""
        home = _battery("home", HOME_CAPACITY, HOME_CAPACITY * 0.50)

        tiny = [_battery(f"tiny_{i}", 1.0, 1.0) for i in range(3)]
        one_big = [_battery("ev", EV_CAPACITY, EV_CAPACITY)]

        gap_tiny = abs(_first_soc_pct(_idle_report([home, *tiny])) - 50.0)
        gap_big = abs(_first_soc_pct(_idle_report([home, *one_big])) - 50.0)

        self.assertLess(gap_tiny, 5.0)
        self.assertGreater(gap_big, 30.0)
        self.assertGreater(
            gap_big,
            gap_tiny,
            "three participants produce a smaller error than one, because "
            "capacity share is what drives it -- not the number of "
            "batteries scored",
        )


class TestTheComparisonItselfIsAsymmetric(unittest.TestCase):
    """The root of #949 in one assertion: the published SoC describes the
    fleet, so no single-sensor reading can match it once a participant is
    scored -- not even a perfect one."""

    def test_no_single_battery_sensor_can_agree_with_the_fleet_number(self):
        home_pct, ev_pct = 50.0, 80.0
        home = _battery("home", HOME_CAPACITY, HOME_CAPACITY * home_pct / 100.0)
        ev = _battery("ev_m3p", EV_CAPACITY, EV_CAPACITY * ev_pct / 100.0)

        published = _first_soc_pct(_idle_report([home, ev]))

        # A perfectly accurate sensor on EITHER pack still disagrees.
        self.assertNotAlmostEqual(published, home_pct, places=3)
        self.assertNotAlmostEqual(published, ev_pct, places=3)
        self.assertGreater(published, min(home_pct, ev_pct))
        self.assertLess(published, max(home_pct, ev_pct))


if __name__ == "__main__":
    unittest.main()
