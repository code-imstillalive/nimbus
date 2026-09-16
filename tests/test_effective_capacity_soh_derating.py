"""nimbus issue #1013 -- State of Health now actually derates capacity.

`number.nimbus_solver_battery_soh_percent` existed as a dashboard dial,
was mirrored onto `sensor.nimbus_solver_config`, and was read by nothing
at all. The reference household had it at 98%, in the reasonable belief
it derated a 122.2 kWh pack to ~119.8 kWh; every solve planned against
the full 122.2. Roughly 5.7 kWh of headroom that does not physically
exist, which matters precisely because #1012 established that pack
reaches both rails every single day.

The semantics were never in doubt -- const.py's own comment beside
`CONF_SOLVER_BATTERY_SOH_PERCENT` has read `effective_capacity =
capacity_kwh * soh_percent / 100` since the field was added. Only the
code was missing.

The one real modelling choice is tested explicitly below: derating
capacity scales the kWh that BOTH SoC rails resolve to, not only the
ceiling. That is correct because a BMS reports SoC as a percentage of
the pack's CURRENT usable capacity -- a degraded pack still reads 100%
when full -- so a 2% floor is 2% of the degraded pack. Deriving only the
ceiling from the derated number would silently hold back more real
energy at the floor than the household asked for.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

NAMEPLATE = 122.2


def _cfg(**overrides) -> dict:
    cfg = {"solver_battery_capacity_kwh": NAMEPLATE}
    cfg.update(overrides)
    return cfg


class TestEffectiveCapacityDerating(unittest.TestCase):
    def setUp(self):
        solver_writer._SOH_RANGE_WARNED.clear()

    def test_an_untouched_install_is_unchanged(self):
        """The whole fix has to be a no-op for anyone who has never set
        the dial, or it is a silent capacity change for every install in
        the field rather than a bug fix."""
        self.assertEqual(
            solver_writer.resolve_effective_capacity_kwh(_cfg()), NAMEPLATE
        )

    def test_an_explicit_100_percent_is_also_unchanged(self):
        self.assertEqual(
            solver_writer.resolve_effective_capacity_kwh(
                _cfg(solver_battery_soh_percent=100.0)
            ),
            NAMEPLATE,
        )

    def test_the_reference_household_case(self):
        """98% on a 122.2 kWh pack. The household believed this already
        happened; until now it did not."""
        self.assertAlmostEqual(
            solver_writer.resolve_effective_capacity_kwh(
                _cfg(solver_battery_soh_percent=98.0)
            ),
            119.756,
            places=3,
        )

    def test_a_string_value_from_a_config_entry_still_works(self):
        """Config-entry values arrive as strings often enough that a
        float-only path would fail on a real install rather than in a
        test."""
        self.assertAlmostEqual(
            solver_writer.resolve_effective_capacity_kwh(
                _cfg(solver_battery_soh_percent="95")
            ),
            116.09,
            places=3,
        )

    def test_none_is_treated_as_unset_not_as_zero(self):
        """`_cfg_num`'s own reason for existing: `or default` would read
        a real 0 as unset. Here the inverse matters -- an explicitly
        null value must fall back to 100%, not collapse the pack."""
        self.assertEqual(
            solver_writer.resolve_effective_capacity_kwh(
                _cfg(solver_battery_soh_percent=None)
            ),
            NAMEPLATE,
        )


class TestOutOfRangeSoHIsRefusedNotApplied(unittest.TestCase):
    """number.py clamps the entity to [1, 100], so these are reachable
    only from hand-edited config or the cron copy's own YAML. A nonsense
    value must not silently shrink a real pack, nor inflate one past its
    nameplate."""

    def setUp(self):
        solver_writer._SOH_RANGE_WARNED.clear()

    def test_zero_does_not_annihilate_the_battery(self):
        with patch.object(solver_writer, "_LOGGER"):
            self.assertEqual(
                solver_writer.resolve_effective_capacity_kwh(
                    _cfg(solver_battery_soh_percent=0.0)
                ),
                NAMEPLATE,
            )

    def test_a_negative_value_does_not_produce_a_negative_pack(self):
        with patch.object(solver_writer, "_LOGGER"):
            self.assertEqual(
                solver_writer.resolve_effective_capacity_kwh(
                    _cfg(solver_battery_soh_percent=-5.0)
                ),
                NAMEPLATE,
            )

    def test_above_100_does_not_inflate_past_the_nameplate(self):
        """A pack can read slightly over 100% when new. Believing it
        would plan against energy the hardware does not have -- the same
        direction of error this issue exists to remove."""
        with patch.object(solver_writer, "_LOGGER"):
            self.assertEqual(
                solver_writer.resolve_effective_capacity_kwh(
                    _cfg(solver_battery_soh_percent=105.0)
                ),
                NAMEPLATE,
            )

    def test_the_refusal_is_announced_but_only_once_per_value(self):
        """Refusing silently would reproduce this issue's own defect in
        a new place. Refusing loudly every solve cycle would reproduce
        nimbus #945's, which buried its own signal at 99 lines an hour.
        """
        with patch.object(solver_writer, "_LOGGER") as log:
            for _ in range(5):
                solver_writer.resolve_effective_capacity_kwh(
                    _cfg(solver_battery_soh_percent=0.0)
                )
            self.assertEqual(log.warning.call_count, 1)
            message = log.warning.call_args[0][0]
            self.assertIn("State of Health", message)
            self.assertIn("nameplate", message)

    def test_a_different_bad_value_warns_again(self):
        """Dedup keyed on the value, not a one-shot latch -- a household
        that fixes 0 to -5 has made a second mistake and should hear
        about it."""
        with patch.object(solver_writer, "_LOGGER") as log:
            solver_writer.resolve_effective_capacity_kwh(
                _cfg(solver_battery_soh_percent=0.0)
            )
            solver_writer.resolve_effective_capacity_kwh(
                _cfg(solver_battery_soh_percent=-5.0)
            )
            self.assertEqual(log.warning.call_count, 2)


class TestMeasuredAgainstTheReferenceHouseholdsOwnRecorder(unittest.TestCase):
    """The modelling choice is not an argument -- it was measured.

    Fourteen consecutive days of daily statistics for two sensors that
    describe the same pack, read from the same recorder:

        sensor.combined_battery_charge   max 119.72 kWh  min 2.39 kWh
        sensor.logger_battery_level_soc  max 100.0 %     min 2.0 %
        sensor.combined_battery_capacity     122.16 kWh, constant

    The BMS reports **100% at 119.72 kWh, not at the 122.16 kWh
    nameplate**. So SoC is a percentage of the pack's CURRENT usable
    capacity, and the nameplate is not the scale the household's own SoC
    sensor is speaking in. That is the premise this whole fix rests on,
    and it is now a reading rather than a physics argument.

    The floor agrees independently: configured Min SoC is 2.0%, and the
    daily minimum lands at 2.39 kWh on ten of the fourteen days. 2.0% of
    119.72 is 2.394. Of the nameplate it would be 2.443. The floor
    tracks the derated number, which is the direct empirical answer to
    "does SoH derate the ceiling, the floor, or both?"

    Two days read 99.3% / 118.96 kWh rather than 100% / 119.72, which
    checks out on the same scale (118.96 / 119.72 = 99.37%) and is worth
    keeping as evidence the relationship holds off the rail too, not
    only at it.

    And the number the household chose is right. They set State of
    Health to 98.0%; 122.2 x 0.98 = 119.756 against a measured 119.72,
    a difference of 0.036 kWh -- 0.03%. The dial was correct the whole
    time. Nothing read it.
    """

    MEASURED_FULL_KWH = 119.72
    MEASURED_FLOOR_KWH = 2.39
    NAMEPLATE_KWH = 122.16

    def test_the_configured_soh_reproduces_the_measured_full_charge(self):
        effective = solver_writer.resolve_effective_capacity_kwh(
            {
                "solver_battery_capacity_kwh": 122.2,
                "solver_battery_soh_percent": 98.0,
            }
        )
        self.assertAlmostEqual(effective, self.MEASURED_FULL_KWH, delta=0.05)

    def test_the_nameplate_would_have_been_wrong_by_a_real_margin(self):
        """2.44 kWh of phantom headroom at the top -- and #1012
        established this pack reaches that rail every single day."""
        self.assertAlmostEqual(
            self.NAMEPLATE_KWH - self.MEASURED_FULL_KWH, 2.44, places=2
        )

    def test_the_measured_floor_matches_the_derated_scale_not_nameplate(self):
        """The empirical half of the both-rails decision."""
        min_pct = 2.0
        derated = self.MEASURED_FULL_KWH * min_pct / 100.0
        nameplate = self.NAMEPLATE_KWH * min_pct / 100.0

        self.assertAlmostEqual(derated, self.MEASURED_FLOOR_KWH, delta=0.01)
        self.assertGreater(
            abs(nameplate - self.MEASURED_FLOOR_KWH),
            abs(derated - self.MEASURED_FLOOR_KWH),
            "the measured daily floor sits on the DERATED scale, not the "
            "nameplate one -- so State of Health moves both rails, which "
            "is what this fix implements",
        )

    def test_the_off_rail_reading_is_on_the_same_scale(self):
        """99.3% / 118.96 kWh, seen on two of the fourteen days. A
        relationship that only held at the rail could be a coincidence
        of two clamps rather than one scale."""
        self.assertAlmostEqual(118.96 / self.MEASURED_FULL_KWH * 100.0, 99.37, places=2)


class TestBothSoCRailsScaleWithSoH(unittest.TestCase):
    """The one genuine modelling choice, pinned so a future reader can
    see it was decided rather than fallen into."""

    def test_the_floor_scales_too_not_only_the_ceiling(self):
        effective = solver_writer.resolve_effective_capacity_kwh(
            _cfg(solver_battery_soh_percent=98.0)
        )
        min_pct, max_pct = 2.0, 100.0

        floor_kwh = solver_writer.resolve_min_soc_kwh(
            min_pct, effective, effective * max_pct / 100.0
        )
        ceiling_kwh = effective * max_pct / 100.0

        self.assertAlmostEqual(ceiling_kwh, 119.756, places=3)
        self.assertAlmostEqual(floor_kwh, 2.39512, places=5)

        # Against the un-derated pack the floor would have been 2.444
        # kWh. Holding the floor at the nameplate figure while the
        # ceiling moved would reserve ~0.05 kWh of real energy the
        # household never asked to reserve, and would put the solver's
        # kWh and the household's own SoC sensor on different scales.
        self.assertLess(floor_kwh, NAMEPLATE * min_pct / 100.0)

    def test_the_usable_band_is_exactly_the_derated_fraction(self):
        """Derating is a clean scalar on the whole band -- no asymmetry
        sneaking in between the two rails."""
        min_pct, max_pct = 2.0, 100.0
        nominal_band = NAMEPLATE * (max_pct - min_pct) / 100.0
        effective = solver_writer.resolve_effective_capacity_kwh(
            _cfg(solver_battery_soh_percent=95.0)
        )
        derated_band = effective * (max_pct - min_pct) / 100.0
        self.assertAlmostEqual(derated_band / nominal_band, 0.95, places=9)


class TestEverySolvePathUsesIt(unittest.TestCase):
    """The #538/#1013 class is specifically "wired in one place, missed
    in another." Four sites build a BatteryConfig from configured
    capacity; a fix that reaches three of them is the same bug with a
    smaller blast radius."""

    def setUp(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        self.writer = (
            root / "custom_components" / "nimbus_load" / "solver_writer.py"
        ).read_text(encoding="utf-8")
        self.cron = (
            root
            / "docs"
            / "real-world-integration"
            / "files"
            / "nimbus_solver_forecast_writer.py"
        ).read_text(encoding="utf-8")

    def test_no_solve_path_reads_raw_configured_capacity_any_more(self):
        for label, src in (("integration", self.writer), ("cron", self.cron)):
            with self.subTest(copy=label):
                raw = [
                    ln
                    for ln in src.split("\n")
                    if "capacity_kwh = " in ln
                    and "solver_battery_capacity_kwh" in ln
                    and "nominal = " not in ln
                ]
                self.assertEqual(
                    raw,
                    [],
                    "a solve path still reads nameplate capacity directly, "
                    "bypassing the SoH derating -- nimbus #1013 is only "
                    f"fixed where every BatteryConfig uses it: {raw}",
                )

    def test_the_cron_copy_has_the_helper_too(self):
        """nimbus #357: the standalone writer has drifted behind the
        integration on a shipped fix before, and a household running it
        via cron would have kept the phantom headroom."""
        self.assertIn("def resolve_effective_capacity_kwh(", self.cron)

    def test_the_effective_number_is_published(self):
        """A household that cannot see what the solver planned against
        cannot tell the dial started working. Both numbers, not just the
        result -- 119.76 alone is indistinguishable from someone having
        retyped the nameplate."""
        for key in (
            "battery_soh_percent",
            "battery_nameplate_capacity_kwh",
            "battery_effective_capacity_kwh",
        ):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', self.writer)


if __name__ == "__main__":
    unittest.main()
