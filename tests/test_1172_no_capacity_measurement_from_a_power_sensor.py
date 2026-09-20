"""nimbus #1172: the scorer must not measure pack capacity from a power
sensor. This test pins an absence.

v0.94.407 shipped `_measured_usable_capacity_kwh()`, which computed

    energy seen at the battery POWER sensor / real-SoC points gained

over the day's largest monotonic rise, and published the quotient as
`measured_usable_capacity_kwh`. v0.94.408 put it on the Regret card:
*"configured usable capacity is N kWh, X% above the M kWh this day's own
charge measures."*

Both are retracted here.

## Why it is wrong in principle

That quotient equals capacity only if the power sensor integrates to
exactly the energy the pack moved. It does not have to, and on real
hardware it does not: any scale error in the sensor, and any conversion
loss between it and the cells, lands wholly in the answer. The function
returns ``capacity x sensor_error`` and contains no term that can
separate the two factors. No threshold, no window selection and no
amount of averaging fixes that -- it is the wrong quantity, not a noisy
estimate of the right one.

## Why it was wrong in practice

Measured on the reference household, 2026-09-20, against the BMS's own
energy counter -- an independent instrument rather than a second view of
the same sensor::

    combined_battery_charge   109.11 -> 21.86 kWh  = -87.25 kWh
    logger_battery_level_soc   91.10 -> 18.20 %    = -72.90 points
    battery power, integrated                      = -82.59 kWh

    usable capacity from the BMS counter   87.25 / 0.729 = 119.7 kWh
    configured (122.16 nameplate x 0.98 SoH)         = 119.72 kWh

The configured value was right to within **0.04 kWh**. The power sensor
under-reads by **5.3%**, so the removed function returned 113.3 kWh on
that window and 109.4 kWh on a charge window -- and told a household
whose pack is configured correctly that it was ~9% too large. Acting on
that would have derated a healthy 122.2 kWh pack, which re-prices live
dispatch.

## How it passed review, which is the part worth remembering

It was checked against its own arithmetic, against a raw-sample
integration of the *same power sensor*, and against four consecutive
days of the *same method*. All three share the defect. It was never
compared against `combined_battery_charge`, which sits on the same
install and answers the question in one subtraction. **Agreement among
several views of one instrument is not corroboration.**

## What would make it sound

An input that reports pack ENERGY directly -- a BMS charge counter.
Nimbus takes no such sensor in config today. Until it does, the honest
output is no measurement at all, not a wrong one.

`configured_usable_capacity_kwh` is deliberately still published: what
the solver plans against is a fact worth stating (#1013), and it was
independently confirmed correct by the reading above.

Source-level, like `test_1067`'s equivalent, because the defect being
guarded is an **absence** -- there is no function left to call.
"""

from __future__ import annotations

import pathlib
import unittest

import _solver_path  # noqa: F401
import solver_writer as sw

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CARD_JS = (
    REPO_ROOT
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-regret-card.js"
)


def _writer_source() -> str:
    return pathlib.Path(sw.__file__.replace(".pyc", ".py")).read_text(encoding="utf-8")


class TestTheFunctionIsGone(unittest.TestCase):
    def test_the_module_exposes_no_capacity_measurement(self):
        self.assertFalse(
            hasattr(sw, "_measured_usable_capacity_kwh"),
            "the retracted capacity measurement is back on the module. It "
            "returns capacity x sensor_error, not capacity -- read this "
            "file's docstring before reinstating it.",
        )

    def test_its_threshold_constant_is_gone_too(self):
        """`_MIN_CAPACITY_RISE_PCT` existed only to make the unsound
        division look trustworthy by refusing small denominators. A
        bigger denominator does not make the quantity correct."""
        self.assertFalse(hasattr(sw, "_MIN_CAPACITY_RISE_PCT"))

    def test_no_call_survives_anywhere_in_the_writer(self):
        src = _writer_source()
        calls = [
            line
            for line in src.splitlines()
            if "_measured_usable_capacity_kwh(" in line
            and not line.lstrip().startswith("#")
        ]
        self.assertEqual(calls, [])


class TestTheFieldIsNoLongerPublished(unittest.TestCase):
    """The attribute is what a household and the cards actually read."""

    def test_the_report_does_not_publish_it(self):
        src = _writer_source()
        published = [
            line
            for line in src.splitlines()
            if '"measured_usable_capacity_kwh"' in line
            and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            published,
            [],
            "`measured_usable_capacity_kwh` is being published again",
        )

    def test_it_is_not_in_the_per_row_history_tuple_either(self):
        self.assertNotIn("measured_usable_capacity_kwh", sw._QUALITY_HISTORY_FIELDS)


class TestTheCardNoLongerTellsHouseholdsTheirPackIsWrong(unittest.TestCase):
    """The card is where the wrong number actually reached someone."""

    @staticmethod
    def _source() -> str:
        return CARD_JS.read_text(encoding="utf-8")

    def test_the_card_does_not_read_the_retracted_field(self):
        code = [
            line
            for line in self._source().splitlines()
            if "measured_usable_capacity_kwh" in line
            and not line.lstrip().startswith("//")
        ]
        self.assertEqual(code, [], "the card reads the retracted field again")

    def test_the_sentence_itself_is_gone(self):
        self.assertNotIn("would account for", self._source())


class TestWhatIsDeliberatelyKEPT(unittest.TestCase):
    """This is a retraction of one unsound measurement, not a rollback of
    everything #1172 touched. A future reader undoing too much is as bad
    as one reinstating the defect."""

    def test_the_configured_capacity_is_still_published(self):
        self.assertIn('"configured_usable_capacity_kwh"', _writer_source())

    def test_it_is_still_the_effective_capacity_not_the_nameplate(self):
        src = _writer_source()
        self.assertIn('"configured_usable_capacity_kwh": round(capacity_kwh, 1)', src)
        self.assertIn("capacity_kwh = resolve_effective_capacity_kwh(cfg)", src)

    def test_the_soh_derate_still_applies(self):
        """119.72 is the figure the BMS counter independently confirmed
        at 119.7 -- if this stops matching, the confirmation no longer
        applies to what is published."""
        self.assertAlmostEqual(
            sw.resolve_effective_capacity_kwh(
                {
                    "solver_battery_capacity_kwh": 122.2,
                    "solver_battery_soh_percent": 98.0,
                }
            ),
            119.756,
            places=2,
        )

    def test_the_reliability_caveat_still_works(self):
        """v0.94.408's real contribution -- telling a household the EPR
        is flagged -- is independent of the capacity sentence and stays."""
        src = CARD_JS.read_text(encoding="utf-8")
        self.assertIn("attrs.epr_reliable === false", src)


class TestTheReasoningIsRecordedWhereItWillBeRead(unittest.TestCase):
    """A deleted function leaves no trace at the place someone would
    naturally re-add it. The comment block is the trace."""

    def test_the_writer_explains_why_there_is_no_measurement(self):
        src = _writer_source()
        self.assertIn(
            "there is deliberately NO capacity measurement here",
            src,
        )
        self.assertIn("Do not reintroduce this from a power sensor", src)

    def test_it_names_this_test_so_the_guard_is_findable(self):
        self.assertIn(
            "test_1172_no_capacity_measurement_from_a_power_sensor",
            _writer_source(),
        )


if __name__ == "__main__":
    unittest.main()
