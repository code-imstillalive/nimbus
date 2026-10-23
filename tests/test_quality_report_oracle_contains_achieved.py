"""nimbus issue #956 -- the oracle must be able to do what the battery
actually did, or regret is not a bound at all.

The reported defect: a real install published **EPR 103.66% with regret
-$0.7307** -- the achieved dispatch pricing out cheaper than perfect
foresight, which the oracle's own construction is supposed to make
impossible.

It is possible because the two sides were not playing the same game. The
oracle is an LP bound by the configured battery envelope and by the P2P
export commitment; the achieved side is a reconstruction from measured
power, bound by neither. On the day in question:

  * the achieved SoC trajectory finished at **0.4429%** against a
    configured 2.0% floor -- 1.90 kWh the oracle was forbidden to sell
    and the reconstruction sold anyway;
  * achieved export ran **12.39-12.77 kW** through 17:00-24:00 against a
    P2P commitment pinned at exactly **12.0 kW**.

Beat an opponent who is not allowed to touch what you just spent, and of
course you win.

**The household chose option B** (2026-09-16, "go with B"): widen the
ORACLE to what the system demonstrably did, rather than clamp the
achieved integration to fit the envelope. Option A would have forced
regret >= 0 by editing what the battery actually did -- changing `j_ach`,
the headline achieved-cost figure, on every install and every rescored
day, and hiding the very signal the anomaly carries. Option B leaves
`j_ach` untouched and fixes the COMPARISON, which is what was broken.

These tests are deliberately split three ways:

  1. the two widening helpers directly, including every byte-identical
     no-op case, since "this changes nothing for a well-behaved day" is
     the load-bearing claim;
  2. `p2p_export.grid_export_bounds()` and `GridConfig.__post_init__`,
     the two places the new band field is actually read and validated;
  3. `compute_quality_report()` end to end on the real #956 shape, which
     is the only level that can show the invariant restored -- and which
     fails against pre-fix code with a genuinely negative regret rather
     than passing vacuously.
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
from solver.p2p_export import grid_export_bounds
from solver.quality_report import (
    _MIN_POSITIVE_SOC_KWH,
    _soc_envelope_containing_achieved,
    _widen_export_pin_to_achieved,
    compute_quality_report,
)

N = 24
HOURS = np.ones(N)

# The reference household's own real scale, not arbitrary test values --
# 122.2 kWh at a 2.0% floor is the exact configuration #956 was reported
# from, and the 1.90 kWh gap the oracle could not sell is only meaningful
# against a real capacity.
CAPACITY = 122.2
MIN_SOC = CAPACITY * 0.02
MAX_SOC = CAPACITY


def _battery(initial_soc_kwh: float = 60.0, **overrides) -> BatteryConfig:
    kwargs = {
        "name": "home",
        "capacity_kwh": CAPACITY,
        "initial_soc_kwh": initial_soc_kwh,
        "min_soc_kwh": MIN_SOC,
        "max_soc_kwh": MAX_SOC,
        "max_charge_kw": 40.0,
        "max_discharge_kw": 40.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    kwargs.update(overrides)
    return BatteryConfig(**kwargs)


class TestSocEnvelopeContainingAchieved(unittest.TestCase):
    """The SoC half of the widening, at the helper."""

    def test_trajectory_inside_the_envelope_changes_nothing(self):
        """The load-bearing no-op claim: an ordinary day, where the
        battery stayed inside its own configured bounds the whole time,
        must come back with exactly the configured numbers -- not
        approximately, exactly."""
        battery = _battery(initial_soc_kwh=60.0)
        # A gentle discharge that never approaches either bound.
        discharge = np.full(N, 1.0)
        floor, ceiling = _soc_envelope_containing_achieved(
            battery, np.zeros(N), discharge, HOURS
        )
        self.assertEqual(floor, MIN_SOC)
        self.assertEqual(ceiling, MAX_SOC)

    def test_achieved_below_the_floor_widens_the_floor_to_it(self):
        """The real #956 case: the reconstruction sells below the
        configured floor, so the floor moves down to meet it."""
        battery = _battery(initial_soc_kwh=MIN_SOC + 1.9)
        # Discharging 1.0 kW for one hour at 95% efficiency removes
        # 1/0.95 = 1.0526 kWh, so two hours takes the trajectory ~0.2 kWh
        # below the floor and it stays there.
        discharge = np.zeros(N)
        discharge[:2] = 1.0
        floor, ceiling = _soc_envelope_containing_achieved(
            battery, np.zeros(N), discharge, HOURS
        )
        self.assertLess(floor, MIN_SOC)
        expected = battery.initial_soc_kwh - 2.0 / 0.95
        self.assertAlmostEqual(floor, expected, places=9)
        self.assertEqual(ceiling, MAX_SOC, "the ceiling has no reason to move")

    def test_achieved_above_the_ceiling_widens_the_ceiling_to_it(self):
        ceiling_configured = MAX_SOC - 5.0
        battery = _battery(
            initial_soc_kwh=ceiling_configured - 1.0, max_soc_kwh=ceiling_configured
        )
        charge = np.zeros(N)
        charge[0] = 2.0
        floor, ceiling = _soc_envelope_containing_achieved(
            battery, charge, np.zeros(N), HOURS
        )
        self.assertGreater(ceiling, ceiling_configured)
        self.assertAlmostEqual(ceiling, ceiling_configured - 1.0 + 2.0 * 0.95, places=9)
        self.assertEqual(floor, MIN_SOC, "the floor has no reason to move")

    def test_a_trajectory_reaching_zero_still_yields_a_positive_floor(self):
        """`BatteryConfig.__post_init__` rejects `min_soc_kwh <= 0`, so a
        reconstruction that genuinely empties the battery still needs a
        representable floor rather than an exception."""
        battery = _battery(initial_soc_kwh=1.0)
        discharge = np.zeros(N)
        discharge[0] = 0.95  # exactly 1.0 kWh out at 95% efficiency
        floor, _ = _soc_envelope_containing_achieved(
            battery, np.zeros(N), discharge, HOURS
        )
        self.assertGreater(floor, 0.0)
        self.assertEqual(floor, _MIN_POSITIVE_SOC_KWH)

    def test_a_trajectory_below_zero_is_clamped_not_honoured(self):
        """Physical bounds still win. A reconstruction leaving [0,
        capacity] is sensor nonsense rather than a state the oracle
        should be asked to match -- that case belongs to the #956
        reliability flag, not to the envelope."""
        battery = _battery(initial_soc_kwh=1.0)
        discharge = np.full(N, 5.0)
        floor, _ = _soc_envelope_containing_achieved(
            battery, np.zeros(N), discharge, HOURS
        )
        self.assertEqual(floor, _MIN_POSITIVE_SOC_KWH)

    def test_a_trajectory_above_capacity_is_clamped_to_capacity(self):
        battery = _battery(initial_soc_kwh=CAPACITY - 1.0)
        charge = np.full(N, 40.0)
        _, ceiling = _soc_envelope_containing_achieved(
            battery, charge, np.zeros(N), HOURS
        )
        self.assertEqual(ceiling, CAPACITY)

    def test_the_widened_envelope_always_builds_a_valid_battery(self):
        """Every widened pair must satisfy `0 < min <= max <= capacity`,
        because the very next thing the scorer does with it is construct
        a BatteryConfig. Swept across trajectories that leave the
        envelope in both directions and in neither."""
        for initial in (0.5, MIN_SOC, 60.0, CAPACITY - 0.5):
            for charge_kw, discharge_kw in (
                (0.0, 0.0),
                (40.0, 0.0),
                (0.0, 40.0),
                (1.0, 1.0),
            ):
                battery = _battery(initial_soc_kwh=max(MIN_SOC, initial))
                floor, ceiling = _soc_envelope_containing_achieved(
                    battery,
                    np.full(N, charge_kw),
                    np.full(N, discharge_kw),
                    HOURS,
                )
                with self.subTest(initial=initial, c=charge_kw, d=discharge_kw):
                    self.assertGreater(floor, 0.0)
                    self.assertLessEqual(floor, ceiling)
                    self.assertLessEqual(ceiling, CAPACITY)
                    # The real proof: it constructs.
                    _battery(
                        initial_soc_kwh=min(
                            max(battery.initial_soc_kwh, floor), ceiling
                        ),
                        min_soc_kwh=floor,
                        max_soc_kwh=ceiling,
                    )


def _grid(fixed_export_kw=None, export_limit_kw: float = 42.0) -> GridConfig:
    return GridConfig(
        import_price=np.full(N, 0.20),
        export_price=np.full(N, 0.10),
        import_limit_kw=42.0,
        export_limit_kw=export_limit_kw,
        fixed_export_kw=fixed_export_kw,
    )


def _pin(value: float, hours: range) -> np.ndarray:
    pin = np.full(N, np.nan)
    for h in hours:
        pin[h] = value
    return pin


class TestWidenExportPinToAchieved(unittest.TestCase):
    """The P2P half of the widening, at the helper."""

    def _widen(self, grid, export_kw):
        """Drive the helper through a load/solar/battery shape that
        reconstructs to exactly `export_kw` of export per period, since
        the helper deliberately reuses regret.py's own reconstruction
        rather than taking an export series directly."""
        return _widen_export_pin_to_achieved(
            grid,
            hours=HOURS,
            load_kw=np.zeros(N),
            solar_kw=np.asarray(export_kw, dtype=np.float64),
            actual_charge_kw=[np.zeros(N)],
            actual_discharge_kw=[np.zeros(N)],
        )

    def test_no_p2p_commitment_returns_the_same_config_untouched(self):
        grid = _grid()
        self.assertIs(self._widen(grid, np.full(N, 5.0)), grid)

    def test_an_all_nan_pin_returns_the_same_config_untouched(self):
        grid = _grid(fixed_export_kw=np.full(N, np.nan))
        self.assertIs(self._widen(grid, np.full(N, 5.0)), grid)

    def test_delivering_the_commitment_exactly_leaves_the_pin_exact(self):
        """Every well-behaved day. lo == hi == pin means
        `grid_export_bounds()` returns the same (pin, pin) it always
        has."""
        grid = _grid(fixed_export_kw=_pin(12.0, range(17, 24)))
        widened = self._widen(grid, _achieved_export(12.0))
        for h in range(17, 24):
            self.assertAlmostEqual(float(widened.fixed_export_kw[h]), 12.0, places=9)
            self.assertAlmostEqual(
                float(widened.fixed_export_max_kw[h]), 12.0, places=9
            )

    def test_over_delivery_raises_the_ceiling_only(self):
        """The real #956 shape: committed 12.0, delivered 12.77."""
        grid = _grid(fixed_export_kw=_pin(12.0, range(17, 24)))
        widened = self._widen(grid, _achieved_export(12.77))
        for h in range(17, 24):
            self.assertAlmostEqual(float(widened.fixed_export_kw[h]), 12.0, places=9)
            self.assertAlmostEqual(
                float(widened.fixed_export_max_kw[h]), 12.77, places=9
            )

    def test_under_delivery_lowers_the_floor_to_what_was_delivered(self):
        """nimbus issue #1001, correcting v0.94.344.

        That release widened upward only, on the stated reasoning that
        under-delivery "makes achieved worse, not better" so it could
        never produce negative regret. That holds while exporting into a
        P2P window is profitable -- which is what a bonus rate is for --
        and fails when the committed export price is a fraction of the
        import price. Then the commitment is a LOSS the oracle cannot
        decline, and a household that quietly skipped it beats perfect
        foresight. See
        `TestUnderDeliveredCommitmentNoLongerBeatsTheOracle` below for
        the measured case.

        The ceiling stays at the commitment, so the 2026-09-01 finding
        (the oracle must not model a fictional market) is untouched --
        that guard lives in test_quality_report_p2p_fixed_export.py and
        still passes."""
        grid = _grid(fixed_export_kw=_pin(12.0, range(17, 24)))
        widened = self._widen(grid, _achieved_export(9.0))
        for h in range(17, 24):
            self.assertAlmostEqual(float(widened.fixed_export_kw[h]), 9.0, places=9)
            self.assertAlmostEqual(
                float(widened.fixed_export_max_kw[h]), 12.0, places=9
            )

    def test_the_floor_never_goes_negative(self):
        """`GridConfig` rejects a pin below 0, and a reconstruction
        cannot export less than nothing -- but a noisy one can compute a
        small negative, so the clamp is asserted rather than assumed."""
        grid = _grid(fixed_export_kw=_pin(12.0, range(17, 24)))
        widened = self._widen(grid, _achieved_export(0.0))
        for h in range(17, 24):
            self.assertGreaterEqual(float(widened.fixed_export_kw[h]), 0.0)
        # And the result must still construct.
        GridConfig(
            import_price=grid.import_price,
            export_price=grid.export_price,
            import_limit_kw=grid.import_limit_kw,
            export_limit_kw=grid.export_limit_kw,
            fixed_export_kw=widened.fixed_export_kw,
            fixed_export_max_kw=widened.fixed_export_max_kw,
        )

    def test_uncommitted_periods_are_left_free(self):
        grid = _grid(fixed_export_kw=_pin(12.0, range(17, 24)))
        widened = self._widen(grid, _achieved_export(12.77))
        for h in range(17):
            self.assertTrue(np.isnan(widened.fixed_export_kw[h]))
            self.assertTrue(np.isnan(widened.fixed_export_max_kw[h]))

    def test_the_ceiling_never_exceeds_the_export_limit(self):
        """A measured export above the contracted limit is sensor or
        unit trouble, not a rate the oracle should be asked to match."""
        grid = _grid(fixed_export_kw=_pin(12.0, range(17, 24)), export_limit_kw=15.0)
        widened = self._widen(grid, _achieved_export(400.0))
        for h in range(17, 24):
            self.assertAlmostEqual(
                float(widened.fixed_export_max_kw[h]), 15.0, places=9
            )
        # And the result must still be a constructible GridConfig.
        GridConfig(
            import_price=grid.import_price,
            export_price=grid.export_price,
            import_limit_kw=grid.import_limit_kw,
            export_limit_kw=grid.export_limit_kw,
            fixed_export_kw=widened.fixed_export_kw,
            fixed_export_max_kw=widened.fixed_export_max_kw,
        )

    def test_the_band_never_inverts(self):
        """lo <= hi must hold on every banded period regardless of what
        the reconstruction produced, since GridConfig rejects an
        inverted band."""
        grid = _grid(fixed_export_kw=_pin(12.0, range(17, 24)), export_limit_kw=12.5)
        for achieved in (0.0, 5.0, 12.0, 12.4, 12.5, 99.0):
            widened = self._widen(grid, _achieved_export(achieved))
            with self.subTest(achieved=achieved):
                band = ~np.isnan(widened.fixed_export_max_kw)
                self.assertTrue(
                    np.all(
                        widened.fixed_export_kw[band]
                        <= widened.fixed_export_max_kw[band]
                    )
                )


def _achieved_export(kw: float) -> np.ndarray:
    """Solar-only export at a flat rate -- the simplest reconstruction
    that produces exactly `kw` of grid export every period."""
    return np.full(N, kw)


class TestGridExportBoundsBand(unittest.TestCase):
    """The one place the new field is actually read."""

    def test_absent_band_is_byte_identical_to_the_exact_pin(self):
        grid = _grid(fixed_export_kw=_pin(12.0, range(17, 24)))
        self.assertEqual(grid_export_bounds(20, grid, 42.0), (12.0, 12.0))

    def test_a_band_widens_only_the_upper_bound(self):
        grid = GridConfig(
            import_price=np.full(N, 0.20),
            export_price=np.full(N, 0.10),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            fixed_export_kw=_pin(12.0, range(17, 24)),
            fixed_export_max_kw=_pin(12.77, range(17, 24)),
        )
        self.assertEqual(grid_export_bounds(20, grid, 42.0), (12.0, 12.77))

    def test_an_unpinned_period_is_unaffected_by_the_band(self):
        grid = GridConfig(
            import_price=np.full(N, 0.20),
            export_price=np.full(N, 0.10),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            fixed_export_kw=_pin(12.0, range(17, 24)),
            fixed_export_max_kw=_pin(12.77, range(17, 24)),
        )
        self.assertEqual(grid_export_bounds(3, grid, 42.0), (0.0, 42.0))

    def test_the_694_spike_override_still_wins_over_a_band(self):
        """#694's own override opens export all the way to the limit for
        period 0; a band must not narrow that back down."""
        grid = GridConfig(
            import_price=np.full(N, 0.20),
            export_price=np.full(N, 0.10),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            fixed_export_kw=_pin(12.0, range(24)),
            fixed_export_max_kw=_pin(12.77, range(24)),
        )
        self.assertEqual(
            grid_export_bounds(0, grid, 42.0, override_p2p=True), (12.0, 42.0)
        )


class TestGridConfigBandValidation(unittest.TestCase):
    def _build(self, **overrides):
        kwargs = {
            "import_price": np.full(N, 0.20),
            "export_price": np.full(N, 0.10),
            "import_limit_kw": 42.0,
            "export_limit_kw": 42.0,
        }
        kwargs.update(overrides)
        return GridConfig(**kwargs)

    def test_a_band_without_a_pin_is_rejected(self):
        with self.assertRaises(ValueError):
            self._build(fixed_export_max_kw=_pin(12.77, range(17, 24)))

    def test_a_wrong_length_band_is_rejected(self):
        with self.assertRaises(ValueError):
            self._build(
                fixed_export_kw=_pin(12.0, range(17, 24)),
                fixed_export_max_kw=np.full(N + 1, np.nan),
            )

    def test_an_inverted_band_is_rejected(self):
        with self.assertRaises(ValueError):
            self._build(
                fixed_export_kw=_pin(12.0, range(17, 24)),
                fixed_export_max_kw=_pin(11.0, range(17, 24)),
            )

    def test_a_band_above_the_export_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            self._build(
                export_limit_kw=12.5,
                fixed_export_kw=_pin(12.0, range(17, 24)),
                fixed_export_max_kw=_pin(20.0, range(17, 24)),
            )

    def test_a_band_that_is_all_nan_is_accepted(self):
        self._build(
            fixed_export_kw=_pin(12.0, range(17, 24)),
            fixed_export_max_kw=np.full(N, np.nan),
        )


class TestRegretIsNonNegativeOnTheReal956Shape(unittest.TestCase):
    """End to end -- the only level that can show the invariant restored.

    Two fixtures, one per asymmetry, each verified to FAIL against
    pre-fix behaviour rather than pass vacuously (the widening helpers
    were stubbed back to the configured bounds and the numbers recorded):

      * sub-floor discharge -- pre-fix **regret -$0.6727, EPR 154.89%**,
        post-fix +$0.1035 / 94.83%;
      * P2P over-delivery -- pre-fix **regret -$0.4424, EPR 101.08%**,
        post-fix +$0.3661 / 99.12%.

    The second lands within a few cents of the real reported -$0.7307 /
    103.66%, from the same shape rather than by tuning toward it.

    Both fixtures deliberately give the battery `max_charge_kw=0`. The
    oracle then has exactly one decision left -- when to spend the energy
    it starts with -- so a cost difference between the two sides can only
    come from the asymmetry under test, never from a charging opportunity
    the achieved day happened not to take.
    """

    START = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)

    def _report(self, *, battery, discharge, grid):
        final_soc = (
            battery.initial_soc_kwh
            - float(np.sum(discharge)) / battery.discharge_efficiency
        )
        zero = np.zeros(N)
        return compute_quality_report(
            periods=PeriodGrid(hours=HOURS, start=self.START),
            grid_residual=grid,
            grid_oracle=grid,
            batteries=[battery],
            solar=SolarConfig(forecast_kw=np.zeros(N)),
            load=LoadConfig(name="house", forecast_kw=np.full(N, 1.0)),
            timestamps=[self.START + timedelta(hours=i) for i in range(N)],
            real_p2p_dollars_earned=0.0,
            commanded_charge_kw=[zero],
            commanded_discharge_kw=[zero],
            actual_charge_kw=[zero],
            actual_discharge_kw=[discharge],
            final_soc_kwh_actual=[final_soc],
        )

    # ---- fixture 1: the achieved day sells below the configured floor

    def _sub_floor_case(self):
        export_price = np.full(N, 0.05)
        export_price[20] = 0.45
        grid = GridConfig(
            import_price=np.full(N, 0.30),
            export_price=export_price,
            import_limit_kw=42.0,
            export_limit_kw=42.0,
        )
        initial = MIN_SOC + 3.0
        # One dump at the day's own best hour, taking the battery to
        # 0.544 kWh -- 0.445% of capacity, the same order as the real
        # install's own 0.4429% against its 2.0% floor.
        discharge = np.zeros(N)
        discharge[20] = (initial - 0.544) * 0.95
        return _battery(initial_soc_kwh=initial, max_charge_kw=0.0), discharge, grid

    def test_the_sub_floor_fixture_genuinely_leaves_the_configured_floor(self):
        """Guards the fixture itself: if this stopped going below the
        floor, every assertion below it would pass vacuously."""
        battery, discharge, _ = self._sub_floor_case()
        trajectory = battery.initial_soc_kwh + np.cumsum(
            -discharge * HOURS / battery.discharge_efficiency
        )
        self.assertLess(float(trajectory.min()), MIN_SOC)

    def test_sub_floor_discharge_no_longer_beats_the_oracle(self):
        battery, discharge, grid = self._sub_floor_case()
        report = self._report(battery=battery, discharge=discharge, grid=grid)
        self.assertGreaterEqual(
            report.epr.uplift_available,
            0.0,
            "the oracle must be able to reach the achieved trajectory, so "
            "achieved can never price out cheaper than perfect foresight",
        )
        self.assertLessEqual(report.epr.epr, 1.0 + 1e-9)

    # ---- fixture 2: the achieved day over-delivers its P2P commitment

    def _over_delivery_case(self):
        export_price = np.full(N, 0.05)
        export_price[17:24] = 0.45
        pin = np.full(N, np.nan)
        pin[17:24] = 12.0
        grid = GridConfig(
            import_price=np.full(N, 0.30),
            export_price=export_price,
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            fixed_export_kw=pin,
        )
        # 12.77 kW of export plus the 1.0 kW house load -- the real
        # install's own upper delivered rate against a 12.0 kW pin.
        discharge = np.zeros(N)
        discharge[17:24] = 13.77
        # Enough SoC that the day ends comfortably above the floor, so
        # this fixture isolates the export pin and says nothing at all
        # about the SoC envelope.
        initial = MIN_SOC + float(np.sum(discharge)) / 0.95 + 5.0
        return _battery(initial_soc_kwh=initial, max_charge_kw=0.0), discharge, grid

    def test_the_over_delivery_fixture_stays_inside_the_soc_envelope(self):
        """Guards the fixture itself, the other way round: this one must
        NOT leave the configured floor, or it would be re-testing the SoC
        widening rather than the export pin."""
        battery, discharge, _ = self._over_delivery_case()
        trajectory = battery.initial_soc_kwh + np.cumsum(
            -discharge * HOURS / battery.discharge_efficiency
        )
        self.assertGreater(float(trajectory.min()), MIN_SOC)

    def test_over_delivering_a_p2p_commitment_no_longer_beats_the_oracle(self):
        battery, discharge, grid = self._over_delivery_case()
        report = self._report(battery=battery, discharge=discharge, grid=grid)
        self.assertGreaterEqual(report.epr.uplift_available, 0.0)
        self.assertLessEqual(report.epr.epr, 1.0 + 1e-9)

    # ---- option B's own promise

    def test_j_ach_is_untouched_by_the_widening(self):
        """Option B's whole point: `j_ach` is the headline achieved-cost
        figure and this fix must not move it. Compared against a direct
        evaluation on the CONFIGURED battery -- exactly what the scorer
        priced before the oracle was widened."""
        from solver.regret import evaluate_realized_cost_multi

        for name, case in (
            ("sub_floor", self._sub_floor_case()),
            ("over_delivery", self._over_delivery_case()),
        ):
            battery, discharge, grid = case
            with self.subTest(fixture=name):
                final_soc = (
                    battery.initial_soc_kwh
                    - float(np.sum(discharge)) / battery.discharge_efficiency
                )
                direct = evaluate_realized_cost_multi(
                    hours=HOURS,
                    load_real_kw=np.full(N, 1.0),
                    solar_real_kw=np.zeros(N),
                    import_price_real=grid.import_price,
                    export_price_real=grid.export_price,
                    batteries=[
                        _battery(
                            initial_soc_kwh=battery.initial_soc_kwh, max_charge_kw=0.0
                        )
                    ],
                    charge_committed_kw=[np.zeros(N)],
                    discharge_committed_kw=[discharge],
                    final_soc_kwh=[final_soc],
                )
                report = self._report(battery=battery, discharge=discharge, grid=grid)
                self.assertAlmostEqual(report.j_ach, direct.total_cost, places=9)


if __name__ == "__main__":
    unittest.main()


class TestUnderDeliveredCommitmentNoLongerBeatsTheOracle(unittest.TestCase):
    """nimbus issue #1001 -- the case v0.94.344 said could not happen.

    Its changelog and code comments both recorded: "it has never been
    observed to go negative from that direction -- under-delivery makes
    achieved WORSE, not better". A rescore of a real day on a
    v0.94.344 install came back at regret -$0.6091 / EPR 103.87% with
    `achieved_within_lp_soc_bounds: true`, which ruled out the SoC half
    and pointed here. Reduced to this fixture, pre-fix it prices out at
    **regret -$4.95** with `j_star` **$2.43 worse than doing nothing**.

    The reasoning failed because exporting into a P2P window is only
    profitable while the committed rate beats spot import. Here export
    is 7.5c against 37c import -- the commitment is a loss the oracle
    cannot decline, and the household simply did not make that trade.
    """

    START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)

    def _case(self):
        import_price = np.full(N, 0.13)
        import_price[17:24] = 0.37
        export_price = np.full(N, 0.02)
        export_price[17:24] = 0.075
        pin = np.full(N, np.nan)
        pin[17:24] = 12.0
        grid = GridConfig(
            import_price=import_price,
            export_price=export_price,
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            fixed_export_kw=pin,
        )
        battery = _battery(initial_soc_kwh=CAPACITY * 0.30)
        # The household essentially ignored the commitment: a little
        # self-consumption, nothing exported in the committed hours.
        discharge = np.zeros(N)
        discharge[17:24] = 1.0
        return battery, discharge, grid

    def _report(self):
        battery, discharge, grid = self._case()
        final_soc = (
            battery.initial_soc_kwh
            - float(np.sum(discharge)) / battery.discharge_efficiency
        )
        zero = np.zeros(N)
        return compute_quality_report(
            periods=PeriodGrid(hours=HOURS, start=self.START),
            grid_residual=grid,
            grid_oracle=grid,
            batteries=[battery],
            solar=SolarConfig(forecast_kw=np.zeros(N)),
            load=LoadConfig(name="house", forecast_kw=np.full(N, 1.0)),
            timestamps=[self.START + timedelta(hours=i) for i in range(N)],
            real_p2p_dollars_earned=0.0,
            commanded_charge_kw=[zero],
            commanded_discharge_kw=[zero],
            actual_charge_kw=[zero],
            actual_discharge_kw=[discharge],
            final_soc_kwh_actual=[final_soc],
        )

    def test_regret_is_not_negative(self):
        report = self._report()
        self.assertGreaterEqual(report.epr.uplift_available, 0.0)

    def test_the_oracle_is_not_worse_than_doing_nothing(self):
        """The sharpest statement of the defect: a perfect-foresight
        oracle that loses to the do-nothing baseline is not an oracle.
        Pre-fix `j_star` was 7.2304 against `j_ref` 4.8000."""
        report = self._report()
        self.assertLessEqual(report.j_star, report.j_ref)

    def test_the_missed_commitment_is_reported_rather_than_lost(self):
        """Dropping the oracle's floor is only defensible because the
        shortfall becomes visible somewhere else. 12 kW committed and
        ~0 delivered across seven hours is ~84 kWh."""
        report = self._report()
        self.assertGreater(report.p2p_commitment_shortfall_kwh, 80.0)

    def test_no_commitment_configured_reports_no_shortfall(self):
        """0.0 must mean "nothing owed", not "not measured"."""
        battery, discharge, grid = self._case()
        grid_no_p2p = GridConfig(
            import_price=grid.import_price,
            export_price=grid.export_price,
            import_limit_kw=grid.import_limit_kw,
            export_limit_kw=grid.export_limit_kw,
        )
        final_soc = (
            battery.initial_soc_kwh
            - float(np.sum(discharge)) / battery.discharge_efficiency
        )
        zero = np.zeros(N)
        report = compute_quality_report(
            periods=PeriodGrid(hours=HOURS, start=self.START),
            grid_residual=grid_no_p2p,
            grid_oracle=grid_no_p2p,
            batteries=[battery],
            solar=SolarConfig(forecast_kw=np.zeros(N)),
            load=LoadConfig(name="house", forecast_kw=np.full(N, 1.0)),
            timestamps=[self.START + timedelta(hours=i) for i in range(N)],
            real_p2p_dollars_earned=0.0,
            commanded_charge_kw=[zero],
            commanded_discharge_kw=[zero],
            actual_charge_kw=[zero],
            actual_discharge_kw=[discharge],
            final_soc_kwh_actual=[final_soc],
        )
        self.assertEqual(report.p2p_commitment_shortfall_kwh, 0.0)
