"""nimbus #1162 ask 2: a scored row carries its own reliability verdict.

#1162 asked what the sensor's `state` should be when `epr_reliable` is
false, and offered two shapes: publish `unknown`, or give the cards a
companion flag to consult.

## Why not `unknown`

The state feeds long-term statistics and the EPR trend chart. Blanking
it puts a hole in the series on precisely the days worth inspecting, and
makes the state flap between a number and nothing -- the appear/vanish
shape #589 was filed about. The number is not the problem.

## What was actually broken

The verdict lived ONLY in the headline attributes, and those describe
`latest_date`. That is why `nimbus-regret-card.js`'s caveat was gated on
`attrs.latest_date === dateKey` when it shipped in v0.94.408: for every
other scored day the card had nothing to read, so it rendered the EPR
bare. One qualified row out of sixty is not a fix for "the dashboard
shows a number the report does not stand behind" -- it is that defect
with one exception.

So the verdict is stamped into the row, beside the five numbers it
qualifies, and travels with them for as long as they are displayed.

## The load-bearing test

`TestTheCodeNeverContradictsTheBoolean`. Two functions now answer "was
this day reliable", from the same three inputs. If they can ever
disagree, the card and the sensor disagree, which is the #1120
headline-and-table defect in a new place. It drives all twelve
combinations (3 SoC states x 2 regret x 2 denominator) rather than
sampling.
"""

from __future__ import annotations

import json
import unittest
from typing import ClassVar

import _solver_path  # noqa: F401
import solver_writer as sw

_code = sw._epr_reliability_code
_FIELD = sw._QUALITY_HISTORY_RELIABILITY_FIELD

_UNRELIABLE_CODES = frozenset(
    {
        sw._RELIABILITY_SOC,
        sw._RELIABILITY_ORACLE,
        sw._RELIABILITY_DENOMINATOR,
        sw._RELIABILITY_UNSTATED,
    }
)


def _entry(soc_reliable, regret_reliable=True, denom_reason=None, epr_reason=None):
    """A day_entry shaped the way `_compute_report_for_window()` builds
    it: the boolean computed by the real function, never hand-set, so the
    two can be compared rather than asserted into agreement."""
    return {
        "epr": 54.13,
        "soc_discrepancy_reliable": soc_reliable,
        "regret_reliable": regret_reliable,
        "epr_denominator_reason": denom_reason,
        "epr_reason": epr_reason,
        "epr_reliable": sw._epr_reliability(
            soc_reliable, regret_reliable, denom_reason
        ),
    }


class TestTheCodeNeverContradictsTheBoolean(unittest.TestCase):
    """Two functions, three inputs, one question. All twelve states."""

    def test_every_combination_agrees(self):
        seen = 0
        for soc in (True, False, None):
            for regret in (True, False):
                for denom in (None, "negative_denominator"):
                    with self.subTest(soc=soc, regret=regret, denom=denom):
                        entry = _entry(soc, regret, denom)
                        verdict = entry["epr_reliable"]
                        got = _code(entry)
                        seen += 1
                        if verdict is True:
                            self.assertEqual(got, sw._RELIABILITY_OK)
                        elif verdict is None:
                            self.assertEqual(got, sw._RELIABILITY_UNKNOWN)
                        else:
                            self.assertIn(
                                got,
                                _UNRELIABLE_CODES,
                                "the row says the day is fine while "
                                "`epr_reliable` says it is not -- the card "
                                "and the sensor would disagree",
                            )
        self.assertEqual(seen, 12, "the combination count is 3 x 2 x 2")

    def test_the_reliable_code_is_never_emitted_for_a_false_verdict(self):
        for soc in (True, False, None):
            for regret in (True, False):
                for denom in (None, "x"):
                    entry = _entry(soc, regret, denom)
                    if entry["epr_reliable"] is False:
                        self.assertNotEqual(_code(entry), sw._RELIABILITY_OK)

    def test_the_codes_are_distinct(self):
        codes = [
            sw._RELIABILITY_OK,
            sw._RELIABILITY_UNKNOWN,
            sw._RELIABILITY_SOC,
            sw._RELIABILITY_ORACLE,
            sw._RELIABILITY_DENOMINATOR,
            sw._RELIABILITY_UNSTATED,
        ]
        self.assertEqual(len(set(codes)), len(codes))
        self.assertTrue(
            all(len(c) == 1 for c in codes), "one character, for #944's cap"
        )


class TestItNamesTheSignalThatDecided(unittest.TestCase):
    """On a day that trips more than one, the named cause must be the one
    `_epr_reliability()` actually returned on -- pointing a household at a
    second, non-deciding signal sends them to the wrong place."""

    def test_a_beaten_oracle_wins_over_everything(self):
        entry = _entry(False, regret_reliable=False, denom_reason="x")
        self.assertEqual(_code(entry), sw._RELIABILITY_ORACLE)

    def test_the_denominator_wins_over_soc(self):
        entry = _entry(False, regret_reliable=True, denom_reason="negative")
        self.assertEqual(_code(entry), sw._RELIABILITY_DENOMINATOR)

    def test_soc_when_it_is_the_only_one(self):
        self.assertEqual(_code(_entry(False)), sw._RELIABILITY_SOC)

    def test_the_precedence_matches_the_boolean_functions_own_order(self):
        """Pins the coupling rather than the behaviour: if
        `_epr_reliability()` is ever reordered, this is the test that
        should fail and force the code function to follow."""
        import inspect

        body = inspect.getsource(sw._epr_reliability)
        i_regret = body.index("if not regret_reliable")
        i_denom = body.index("if epr_denominator_reason is not None")
        i_soc = body.index("if soc_discrepancy_reliable is None")
        self.assertLess(i_regret, i_denom)
        self.assertLess(i_denom, i_soc)

    def test_an_soc_reason_string_is_enough_on_its_own(self):
        """The rescore/fast path re-reads published attributes, where
        `soc_discrepancy_reliable` may be absent but `epr_reason` is
        not."""
        entry = {
            "epr_reliable": False,
            "epr_reason": "achieved_soc_unreliable:disagreement",
        }
        self.assertEqual(_code(entry), sw._RELIABILITY_SOC)

    def test_a_false_verdict_with_nothing_naming_it_still_says_so(self):
        """Never silence. A day flagged with no surviving cause reads as
        flagged-cause-unknown, not as clean."""
        self.assertEqual(_code({"epr_reliable": False}), sw._RELIABILITY_UNSTATED)


class TestAbsenceKeepsMeaningOneThing(unittest.TestCase):
    """A row written before this existed must stay distinguishable from a
    reliable one -- otherwise the whole pre-#1162 table silently reads as
    verified."""

    def test_an_entry_with_no_verdict_gets_no_code(self):
        self.assertIsNone(_code({"epr": 54.13, "j_ref": 1.0}))

    def test_and_therefore_no_key_is_written(self):
        history = sw._carry_forward_quality_history({}, "2026-09-19", {"epr": 54.13})
        self.assertNotIn(_FIELD, history["2026-09-19"])


class TestItIsWrittenIntoTheRow(unittest.TestCase):
    def test_a_scored_day_carries_its_verdict(self):
        entry = _entry(False)
        history = sw._carry_forward_quality_history({}, "2026-09-19", entry)
        self.assertEqual(history["2026-09-19"][_FIELD], sw._RELIABILITY_SOC)

    def test_a_reliable_day_is_stamped_too_rather_than_left_blank(self):
        entry = _entry(True)
        history = sw._carry_forward_quality_history({}, "2026-09-19", entry)
        self.assertEqual(history["2026-09-19"][_FIELD], sw._RELIABILITY_OK)

    def test_prior_rows_are_not_back_stamped(self):
        """Same rule as #1120's version stamp: a row this code did not
        score must not be given a verdict it never produced."""
        prior = {"history": {"2026-09-18": {"epr": 70.6}}}
        history = sw._carry_forward_quality_history(prior, "2026-09-19", _entry(False))
        self.assertNotIn(_FIELD, history["2026-09-18"])
        self.assertIn(_FIELD, history["2026-09-19"])

    def test_it_rides_beside_the_version_stamp_not_instead_of_it(self):
        history = sw._carry_forward_quality_history({}, "2026-09-19", _entry(False))
        row = history["2026-09-19"]
        self.assertIn(_FIELD, row)
        self.assertIn(sw._QUALITY_HISTORY_VERSION_FIELD, row)


class TestTheByteBudget(unittest.TestCase):
    """The stated reason the key is one character. #944 measured the
    payload at 20,738 bytes against the recorder's 16,384 cap, so a new
    per-row field has to be cheap enough to justify in the same terms."""

    # Compact separators, because that is what actually goes on the wire
    # and what the ~480-byte claim in the source refers to. Measuring
    # `json.dumps()`'s pretty default instead reads 600 -- the first
    # version of this test did, and was comparing a number against a
    # claim made about a different encoding.
    _WIRE: ClassVar[dict] = {"separators": (",", ":")}

    def test_a_full_table_costs_under_500_bytes(self):
        history = {}
        for day in range(sw._QUALITY_HISTORY_MAX_DAYS):
            history[f"2026-07-{day + 1:02d}"] = {"epr": 54.13}
        bare = len(json.dumps(history, **self._WIRE))
        for row in history.values():
            row[_FIELD] = sw._RELIABILITY_SOC
        cost = len(json.dumps(history, **self._WIRE)) - bare
        self.assertLess(
            cost,
            500,
            f"{cost} bytes for 60 rows -- the one-character key's own "
            "justification no longer holds",
        )

    def test_it_is_cheaper_than_the_version_stamp_it_sits_beside(self):
        """Keeps the budget claim comparative rather than absolute: `"v"`
        was accepted at ~960 bytes for the same 60 rows, so this field
        being smaller than that is the standard it has to meet."""
        history = {f"2026-07-{d + 1:02d}": {} for d in range(60)}
        bare = len(json.dumps(history, **self._WIRE))
        version = {k: {sw._QUALITY_HISTORY_VERSION_FIELD: "0.94.412"} for k in history}
        verdict = {k: {_FIELD: sw._RELIABILITY_SOC} for k in history}
        self.assertLess(
            len(json.dumps(verdict, **self._WIRE)) - bare,
            len(json.dumps(version, **self._WIRE)) - bare,
        )


class TestTheCardKnowsEveryCode(unittest.TestCase):
    """The drift guard, and the reason a code table is acceptable at all.

    The writer emits these and `nimbus-regret-card.js` renders them, in
    two languages with no shared definition. A code added on one side and
    not the other is how a household ends up reading "the report did not
    say why" on a day the report said exactly why.
    """

    @staticmethod
    def _card_source() -> str:
        from pathlib import Path

        return (
            Path(sw.__file__).resolve().parent / "frontend" / "nimbus-regret-card.js"
        ).read_text(encoding="utf-8")

    @classmethod
    def _row_fn(cls) -> str:
        src = cls._card_source()
        start = src.index("_rowCaveatFor(dateKey, attrs) {")
        end = src.index("\n  _caveatFor(dateKey, attrs) {", start)
        return src[start:end]

    def test_every_code_appears_in_the_card(self):
        fn = self._row_fn()
        for name, code in (
            ("OK", sw._RELIABILITY_OK),
            ("UNKNOWN", sw._RELIABILITY_UNKNOWN),
            ("SOC", sw._RELIABILITY_SOC),
            ("ORACLE", sw._RELIABILITY_ORACLE),
            ("DENOMINATOR", sw._RELIABILITY_DENOMINATOR),
        ):
            with self.subTest(code=name):
                self.assertIn(
                    f'"{code}"',
                    fn,
                    f"the writer can emit {code!r} ({name}) and the card has "
                    "no branch for it",
                )

    def test_the_card_reads_the_row_field_by_its_real_name(self):
        self.assertIn(f"row.{_FIELD}", self._card_source())

    def test_an_unrecognised_code_still_surfaces(self):
        """A newer writer's cause must read as flagged, not as clean."""
        self.assertIn('why = "the report did not say why"', self._row_fn())

    def test_the_card_reads_the_row_and_not_the_headline(self):
        """Reaching for a headline attribute in the per-row path is the
        cross-day confusion the `latest_date` gate exists to prevent."""
        fn = self._row_fn()
        for forbidden in (
            "attrs.epr_reliable",
            "attrs.epr_reason",
            "attrs.epr_denominator_reason",
            "attrs.regret_path_delta_share",
        ):
            self.assertNotIn(forbidden, fn, f"{forbidden} describes latest_date only")


if __name__ == "__main__":
    unittest.main()
