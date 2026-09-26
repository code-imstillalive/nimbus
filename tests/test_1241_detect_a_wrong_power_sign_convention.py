"""nimbus issue #1241: check a declared battery power-sign convention against
recorded history, and report rather than auto-correct.

## What makes this checkable

The declaration is **redundant with recorded history**, and that redundancy is
the test. Over a window where SoC rose, energy went in; where it fell, energy
came out. So the sign of the mean power across that window is determined, not
assumed.

Both series are already required and already fetched by the scorer, so nothing
new is configured — this compares two things the code already has.

## Why it reports and never corrects

> **Report, do not auto-correct.** Silently flipping a household's declared
> convention because a heuristic disagreed with it is the kind of "fix" that
> becomes the next investigation.

`TestItNeverCorrects` pins that there is no code path returning a corrected
sign — the result carries a verdict and its evidence, and the decision stays
with a human.

## Why INSUFFICIENT_EVIDENCE is a first-class answer

A flat pack is not evidence. Neither is one window, which can be confounded by
a recorder gap (#1161) or a wake transient (#843). Nor is a genuinely mixed
history — that is not a sign error, it is a sign that something else is wrong
with one of the two series, and saying so is more useful than picking the
majority of a coin flip.

## The four-case truth table

The whole detector rests on one line, so every case is tested explicitly
rather than by sampling:

| SoC | mean power | implies |
|---|---|---|
| rose | positive | positive = charge |
| rose | negative | positive = discharge |
| fell | positive | positive = discharge |
| fell | negative | positive = charge |
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nimbus_load"
    / "solver_inputs"
    / "sign_convention.py"
)


def _load_module():
    """Load by path, with no Home Assistant import anywhere.

    Deliberately not a package import: that executes the package `__init__`,
    which pulls in HA's config-entry machinery. This module has zero HA
    dependencies and this loader is what PROVES it -- if an HA import is ever
    added, this file fails first. Same pattern as
    `test_467_calendar_trip_windows.py`, for the same reason.
    """
    spec = importlib.util.spec_from_file_location("sign_convention", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `dataclasses` resolves annotations through
    # `sys.modules[cls.__module__]` and raises AttributeError without it.
    sys.modules["sign_convention"] = module
    spec.loader.exec_module(module)
    return module


sc = _load_module()
SignConventionResult = sc.SignConventionResult
SignVerdict = sc.SignVerdict
detect_power_sign_convention = sc.detect_power_sign_convention

# tz-AWARE on purpose: HA's recorder hands back aware datetimes, and a
# naive fixture would test a shape production never sees. Brisbane,
# matching the reference household.
BNE = timezone(timedelta(hours=10))
T0 = datetime(2026, 9, 20, 0, 0, tzinfo=BNE)


def _series(soc_steps: list[float], power_per_window: list[float]):
    """Build an aligned (soc, power) pair of series.

    `soc_steps` are absolute SoC values; `power_per_window` is one constant
    power reading for each interval between them.
    """
    soc = [(T0 + timedelta(hours=i), v) for i, v in enumerate(soc_steps)]
    power = []
    for i, p in enumerate(power_per_window):
        # two samples inside each window, so the mean is unambiguous
        power.append((T0 + timedelta(hours=i, minutes=10), p))
        power.append((T0 + timedelta(hours=i, minutes=40), p))
    return soc, power


class TestTheFourCaseTruthTable(unittest.TestCase):
    """One line decides everything, so every case is asserted directly."""

    def _verdict(self, soc_steps, powers, declared):
        soc, power = _series(soc_steps, powers)
        return detect_power_sign_convention(
            soc, power, declared_positive_is_charge=declared, min_windows=1
        ).verdict

    def test_soc_rose_and_power_positive_means_positive_is_charge(self):
        self.assertIs(self._verdict([50, 60], [5.0], declared=True), SignVerdict.AGREES)
        self.assertIs(
            self._verdict([50, 60], [5.0], declared=False), SignVerdict.DISAGREES
        )

    def test_soc_rose_and_power_negative_means_positive_is_discharge(self):
        self.assertIs(
            self._verdict([50, 60], [-5.0], declared=False), SignVerdict.AGREES
        )
        self.assertIs(
            self._verdict([50, 60], [-5.0], declared=True), SignVerdict.DISAGREES
        )

    def test_soc_fell_and_power_positive_means_positive_is_discharge(self):
        self.assertIs(
            self._verdict([60, 50], [5.0], declared=False), SignVerdict.AGREES
        )
        self.assertIs(
            self._verdict([60, 50], [5.0], declared=True), SignVerdict.DISAGREES
        )

    def test_soc_fell_and_power_negative_means_positive_is_charge(self):
        self.assertIs(
            self._verdict([60, 50], [-5.0], declared=True), SignVerdict.AGREES
        )
        self.assertIs(
            self._verdict([60, 50], [-5.0], declared=False), SignVerdict.DISAGREES
        )


class TestARealisticWrongDeclaration(unittest.TestCase):
    def test_a_full_day_of_charge_and_discharge_catches_it(self):
        """A pack that charges through the day and discharges at night, with
        the convention declared backwards."""
        soc, power = _series(
            [20, 40, 60, 80, 90, 70, 50, 30],
            [6.0, 6.0, 6.0, 4.0, -8.0, -8.0, -8.0],
        )
        got = detect_power_sign_convention(
            soc, power, declared_positive_is_charge=False
        )
        self.assertIs(got.verdict, SignVerdict.DISAGREES)
        self.assertEqual(got.windows_disagreeing, 7)
        self.assertEqual(got.windows_agreeing, 0)
        self.assertTrue(got.is_actionable)

    def test_the_same_history_declared_correctly_agrees(self):
        soc, power = _series(
            [20, 40, 60, 80, 90, 70, 50, 30],
            [6.0, 6.0, 6.0, 4.0, -8.0, -8.0, -8.0],
        )
        got = detect_power_sign_convention(soc, power, declared_positive_is_charge=True)
        self.assertIs(got.verdict, SignVerdict.AGREES)
        self.assertFalse(got.is_actionable)


class TestWhatIsDeliberatelyNotEvidence(unittest.TestCase):
    def test_a_flat_pack_yields_no_verdict(self):
        """A pack at 100% in absorption draws real power with no SoC
        movement. Counting that would make the detector confidently wrong."""
        soc, power = _series([80.0, 80.2, 80.1, 80.3, 80.2], [3.0, 3.0, 3.0, 3.0])
        got = detect_power_sign_convention(soc, power, declared_positive_is_charge=True)
        self.assertIs(got.verdict, SignVerdict.INSUFFICIENT_EVIDENCE)
        self.assertEqual(got.windows_examined, 0)

    def test_near_zero_power_says_nothing_about_direction(self):
        soc, power = _series([50, 60, 70, 80, 90], [0.01, 0.01, 0.01, 0.01])
        got = detect_power_sign_convention(soc, power, declared_positive_is_charge=True)
        self.assertIs(got.verdict, SignVerdict.INSUFFICIENT_EVIDENCE)

    def test_too_few_windows_is_not_a_finding(self):
        """One window can be a recorder gap (#1161) or a wake transient
        (#843). The default floor is four."""
        soc, power = _series([50, 60], [5.0])
        got = detect_power_sign_convention(
            soc, power, declared_positive_is_charge=False
        )
        self.assertIs(got.verdict, SignVerdict.INSUFFICIENT_EVIDENCE)
        self.assertIn("usable window", got.reason or "")

    def test_a_genuinely_mixed_history_is_reported_as_mixed(self):
        """Half agreeing is not a sign error -- it means something else is
        wrong with one of the two series, and picking the majority of a coin
        flip would bury that."""
        soc, power = _series([50, 60, 50, 60, 50, 60], [5.0, 5.0, -5.0, -5.0, 5.0])
        got = detect_power_sign_convention(
            soc, power, declared_positive_is_charge=True, min_windows=1
        )
        self.assertIs(got.verdict, SignVerdict.INSUFFICIENT_EVIDENCE)
        self.assertIn("mixed", (got.reason or "").lower())

    def test_empty_history_does_not_raise(self):
        got = detect_power_sign_convention([], [], declared_positive_is_charge=True)
        self.assertIs(got.verdict, SignVerdict.INSUFFICIENT_EVIDENCE)


class TestAMinorityOfBadWindowsDoesNotFlipTheVerdict(unittest.TestCase):
    def test_one_confounded_window_among_many_good_ones(self):
        """#843's wake transient and #1161's recorder gap both produce a
        single odd window. The majority rule exists for exactly that."""
        soc, power = _series(
            [10, 20, 30, 40, 50, 60, 70, 80, 90],
            [5.0, 5.0, 5.0, -5.0, 5.0, 5.0, 5.0, 5.0],
        )
        got = detect_power_sign_convention(soc, power, declared_positive_is_charge=True)
        self.assertIs(got.verdict, SignVerdict.AGREES)
        self.assertEqual(got.windows_disagreeing, 1)


class TestItNeverCorrects(unittest.TestCase):
    """The issue is explicit: report, do not auto-correct. The precedent is
    `nuc_state_reconcile.py`, whose --mode reconcile deliberately behaves
    identically to --mode audit."""

    def test_the_result_carries_no_corrected_value(self):
        fields = set(SignConventionResult.__dataclass_fields__)
        for forbidden in ("corrected_sign", "should_flip", "suggested_value"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, fields)

    def test_the_module_writes_nothing(self):
        src = inspect.getsource(sys.modules[detect_power_sign_convention.__module__])
        for writer in ("async_set", "hass.", "config_entries", "async_update_entry"):
            with self.subTest(writer=writer):
                self.assertNotIn(writer, src)

    def test_only_a_confident_disagreement_is_actionable(self):
        """A caller must not warn on "we could not tell"."""
        for verdict, expected in (
            (SignVerdict.DISAGREES, True),
            (SignVerdict.AGREES, False),
            (SignVerdict.INSUFFICIENT_EVIDENCE, False),
        ):
            with self.subTest(verdict=verdict):
                self.assertIs(
                    SignConventionResult(verdict=verdict).is_actionable, expected
                )


class TestItDistinguishesASignErrorFromAMagnitudeOne(unittest.TestCase):
    """#1231 is a real ~5-8% offset in BOTH directions and also shows up as an
    energy imbalance. A wrong sign inverts the split; a plane offset scales
    both the same way. The issue asked for that distinction explicitly."""

    def test_a_scaled_but_correctly_signed_history_still_AGREES(self):
        """Every reading 8% low, sign intact -- that is #1231, not #1241, and
        this detector must not claim it."""
        soc, power = _series(
            [10, 30, 50, 70, 90, 70, 50, 30],
            [5.52, 5.52, 5.52, 5.52, -7.36, -7.36, -7.36],
        )
        got = detect_power_sign_convention(soc, power, declared_positive_is_charge=True)
        self.assertIs(got.verdict, SignVerdict.AGREES)

    def test_the_magnitude_is_reported_for_the_caller_that_wants_it(self):
        soc, power = _series([10, 30, 50, 70, 90], [4.0, 4.0, 4.0, 4.0])
        got = detect_power_sign_convention(soc, power, declared_positive_is_charge=True)
        self.assertAlmostEqual(got.mean_abs_power_kw or 0.0, 4.0, places=6)


class TestItIsPure(unittest.TestCase):
    """No HA imports, same as solver_inputs/calendar_trips.py -- so it is
    testable without a running Home Assistant and reusable from the cron
    writer path as well as the native one."""

    def test_no_homeassistant_import(self):
        src = inspect.getsource(sys.modules[detect_power_sign_convention.__module__])
        self.assertNotIn("homeassistant", src)

    def test_unsorted_input_is_handled(self):
        soc, power = _series([10, 30, 50, 70, 90], [4.0, 4.0, 4.0, 4.0])
        got = detect_power_sign_convention(
            list(reversed(soc)),
            list(reversed(power)),
            declared_positive_is_charge=True,
        )
        self.assertIs(got.verdict, SignVerdict.AGREES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
