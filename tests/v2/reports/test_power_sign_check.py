"""Checking a declared battery power sign against recorded history: over a
window where SoC rose, energy went in. Reported, never corrected; too little
or mixed evidence is an answer of its own, and a magnitude error is not a
sign error (#1231, #1241) (#1837).
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
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

    def test_soc_fell_and_power_negative_means_positive_is_charge(self):
        self.assertIs(
            self._verdict([60, 50], [-5.0], declared=True), SignVerdict.AGREES
        )
        self.assertIs(
            self._verdict([60, 50], [-5.0], declared=False), SignVerdict.DISAGREES
        )


class TestWhatIsDeliberatelyNotEvidence(unittest.TestCase):
    def test_near_zero_power_says_nothing_about_direction(self):
        soc, power = _series([50, 60, 70, 80, 90], [0.01, 0.01, 0.01, 0.01])
        got = detect_power_sign_convention(soc, power, declared_positive_is_charge=True)
        self.assertIs(got.verdict, SignVerdict.INSUFFICIENT_EVIDENCE)

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


class TestItNeverCorrects(unittest.TestCase):
    """The issue is explicit: report, do not auto-correct. The precedent is
    `nuc_state_reconcile.py`, whose --mode reconcile deliberately behaves
    identically to --mode audit."""

    def test_the_result_carries_no_corrected_value(self):
        fields = set(SignConventionResult.__dataclass_fields__)
        for forbidden in ("corrected_sign", "should_flip", "suggested_value"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, fields)


if __name__ == "__main__":
    unittest.main(verbosity=2)
