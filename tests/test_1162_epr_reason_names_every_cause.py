"""nimbus #1162: when `epr_reliable` is not True, something must say why.

`_epr_reliability()` combines three signals. Until this file, only two of
them could be traced back from the published report:

    signal                      makes epr_reliable    reason a reader finds
    -------------------------   -------------------   ---------------------
    regret_reliable             False                 epr_reason
    epr_denominator_reason      False                 its own field
    soc_discrepancy_reliable    False / None          nothing

Measured on the reference household, 2026-09-20, scoring 2026-09-19::

    epr_reliable              false
    epr_reason                null
    epr_denominator_reason    null
    regret_reliable           true
    soc_discrepancy_reliable  false
    soc_discrepancy_reason    "disagreement"

Every field naming EPR said nothing was wrong while the flag said the
number could not be read as a measurement. The cause was real: the
achieved reconstruction had drifted 19.11 points (~20 kWh) from the real
SoC sensor. `soc_discrepancy_reason` recorded it, but nothing tied that
field to the EPR flag.

`test_quality_report_negative_regret_guard.py` already pins
`_epr_reliability(False, True) is False` -- the exact live combination.
The flag was tested; the *reason* never was, which is how a
reason-producing gap survives a suite that covers the flag.

## The invariant

**`epr_reliable is not True` implies at least one reason field is
non-None.** That is what was violated, it is checkable over the whole
signal space rather than on one example, and it is what this file exists
to hold. Asserted exhaustively below rather than on the single measured
day, because the measured day is one point in a 3x3 space and the gap
lived in a cell nothing had visited.

## What is deliberately preserved

- `regret_reliable`'s own labels win when both fire. They are the
  stronger statement and a history of them stays readable.
- The denominator cause is **not** mirrored into `epr_reason`.
  `_epr_reliability()`'s own docstring records that decision: the two
  describe different halves of the same ratio and a reader needs to see
  both on a day where both happen. A test below pins that it stays
  separate, so folding it in later is a deliberate choice.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import _solver_path  # noqa: F401
import solver_writer

_SOC_STATES = (True, False, None)
_REGRET_STATES = (True, False)
_DENOM_STATES = (None, "denominator_not_positive")


class TestTheMeasuredDay(unittest.TestCase):
    """The exact combination read off the live sensor."""

    def test_the_live_combination_now_has_a_reason(self):
        self.assertIs(solver_writer._epr_reliability(False, True, None), False)
        reason = solver_writer._epr_soc_reason(False, "disagreement")
        self.assertIsNotNone(
            reason,
            "epr_reliable is False on this combination and every field naming "
            "EPR still reads None -- the household sees 'do not trust this' "
            "with nothing to act on",
        )
        self.assertIn("disagreement", reason)

    def test_the_reason_points_at_the_soc_half(self):
        """A reader has to land on the right attribute, not merely learn
        that something is wrong."""
        self.assertTrue(
            solver_writer._epr_soc_reason(False, "disagreement").startswith(
                "achieved_soc_"
            )
        )

    def test_it_carries_the_soc_verdict_rather_than_restating_it(self):
        """Two fields inventing their own wording for one finding is how
        they drift into disagreeing about it."""
        for verdict in ("disagreement", "insufficient_overlap", "sensor_missing"):
            with self.subTest(verdict=verdict):
                self.assertIn(verdict, solver_writer._epr_soc_reason(False, verdict))


class TestUnknownIsNotTheSameAsWrong(unittest.TestCase):
    def test_unverifiable_is_named_distinctly(self):
        """`_epr_reliability()` propagates the unknown SoC case as None
        rather than False. The reason has to make the same distinction or
        a reader cannot tell 'we checked and it disagrees' from 'we could
        not check'."""
        self.assertIsNone(solver_writer._epr_reliability(None, True, None))
        self.assertEqual(
            solver_writer._epr_soc_reason(None), "achieved_soc_unverifiable"
        )

    def test_the_two_soc_cases_do_not_share_a_label(self):
        self.assertNotEqual(
            solver_writer._epr_soc_reason(None),
            solver_writer._epr_soc_reason(False, "disagreement"),
        )

    def test_a_missing_soc_verdict_still_produces_a_reason(self):
        """`soc_discrepancy_reason` can itself be absent. Returning None
        here would reopen the exact gap."""
        self.assertIsNotNone(solver_writer._epr_soc_reason(False, None))


class TestAHealthyDaySaysNothing(unittest.TestCase):
    def test_a_reliable_soc_half_contributes_no_reason(self):
        self.assertIsNone(solver_writer._epr_soc_reason(True, None))
        self.assertIsNone(solver_writer._epr_soc_reason(True, "disagreement"))

    def test_a_fully_healthy_day_is_reliable(self):
        self.assertIs(solver_writer._epr_reliability(True, True, None), True)


class TestTheInvariantOverTheWholeSignalSpace(unittest.TestCase):
    """The rule, checked on all 12 signal combinations rather than on
    the one day that happened to expose it.

    3 SoC states x 2 regret states x 2 denominator states. Exactly one of
    the twelve is reliable -- everything healthy -- so eleven must carry
    a reason, and the live defect was one of those eleven."""

    @staticmethod
    def _published(soc, regret, denom, soc_reason="disagreement"):
        """Reproduce what the report publishes for a signal combination,
        including the fallback the publish site applies."""
        reliable = solver_writer._epr_reliability(soc, regret, denom)
        # `_achieved_feasibility_stats()`'s own rule, which owns the field
        # first and is left untouched by #1162.
        reason = None if regret else "oracle_beaten"
        if reason is None:
            reason = solver_writer._epr_soc_reason(soc, soc_reason)
        return reliable, reason, denom

    def test_not_true_always_has_a_reason(self):
        checked = 0
        for soc in _SOC_STATES:
            for regret in _REGRET_STATES:
                for denom in _DENOM_STATES:
                    reliable, reason, d = self._published(soc, regret, denom)
                    if reliable is True:
                        continue
                    checked += 1
                    with self.subTest(soc=soc, regret=regret, denom=denom):
                        self.assertTrue(
                            reason is not None or d is not None,
                            f"epr_reliable={reliable!r} with no reason anywhere "
                            f"(soc={soc!r}, regret={regret!r}, denom={d!r}) -- "
                            "the household is told not to trust the number and "
                            "given nothing to act on",
                        )
        # 12 combinations, exactly one of which is reliable. Pinned as
        # equality rather than a floor: if a future signal changes which
        # combinations are unreliable, this file's premise needs re-reading
        # rather than silently covering fewer cases.
        self.assertEqual(
            checked,
            11,
            "the sweep no longer exercises exactly the unreliable cases -- "
            "recount before adjusting this number",
        )

    def test_a_true_day_carries_no_reason(self):
        reliable, reason, denom = self._published(True, True, None)
        self.assertIs(reliable, True)
        self.assertIsNone(reason)
        self.assertIsNone(denom)

    def test_the_regret_label_wins_when_both_fire(self):
        _reliable, reason, _d = self._published(False, False, None)
        self.assertEqual(
            reason,
            "oracle_beaten",
            "a negative regret is the stronger finding and its labels have "
            "history -- the SoC fallback must not displace them",
        )

    def test_the_denominator_cause_keeps_its_own_field(self):
        """Pinned so folding it into `epr_reason` later is a deliberate
        decision with a test to change, per `_epr_reliability()`'s own
        recorded reasoning."""
        _reliable, reason, denom = self._published(
            True, True, "denominator_not_positive"
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(denom)


class TestItIsActuallyWiredIn(unittest.TestCase):
    """A correct helper nothing calls is the failure shape this issue's
    own siblings keep taking. Source-checked for the same reason
    #1109/#1111's equivalents are: the defect is an absence, and driving
    it needs a real scored day whose SoC comparison genuinely fails."""

    def test_the_publish_path_calls_the_helper(self):
        source = Path(solver_writer.__file__.replace(".pyc", ".py")).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_compute_report_for_window"
        )
        called = {
            (
                n.func.attr
                if isinstance(n.func, ast.Attribute)
                else getattr(n.func, "id", "")
            )
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
        }
        self.assertIn(
            "_epr_soc_reason",
            called,
            "the report no longer fills epr_reason from the SoC half, so "
            "epr_reliable can go False with every reason field reading None "
            "again (#1162)",
        )

    def test_the_fallback_does_not_clobber_an_existing_reason(self):
        """The guard around the call, not just the call."""
        source = Path(solver_writer.__file__.replace(".pyc", ".py")).read_text(
            encoding="utf-8"
        )
        idx = source.index("_epr_soc_reason(\n            soc_discrepancy")
        preceding = source[:idx]
        self.assertIn(
            'if achieved_feasibility.get("epr_reason") is None:',
            preceding[-400:],
            "the SoC fallback is no longer guarded, so it can overwrite a "
            "regret finding that is the more serious of the two",
        )


if __name__ == "__main__":
    unittest.main()
