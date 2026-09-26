"""Regret card: an on-demand re-score control, and the missing-row repair.

## What existed, and the good reason it was narrow

#1200 put a "Re-score with settlement" button on this card, gated on
`provisional` — the day was scored before its P2P settlement landed. #1208
(Mark Purcell) then fixed its cost: it asks for the **day** (`date:`) rather
than a look-back (`days:`), so repairing a ten-day-old row costs one oracle
solve instead of ten.

The gate carried an explicit reason, and it is a good one:

    offer the repair right where the figures are, and ONLY when there is
    something to repair -- a button that is always there invites a household
    to buy oracle solves on a day that is already correct.

## The two gaps

**1. "Missing from the table" is something to repair.** That case did not exist
when #1200 was written; #1248 created it. Measured on the reference household
2026-09-26: a degraded recorder window truncated the table from **10 rows to 1**,
and the card then showed a live estimate for all nine lost days — correctly
labelled *"not yet in table, showing live estimate"* — with no way to rebuild
any of them.

A missing row is strictly worse than a provisional one: a provisional row is
present but priced P2P-blind, whereas a missing row means the card is showing a
live estimate that can disagree with the table outright, measured at EPR
**117.8% live against 103.66% stored** on the same day. That disagreement is the
thing this card exists to expose.

**2. There was no on-demand control at all.** Every path to a re-score was
conditional on something being wrong. A household that simply wanted to re-score
a day — after a deploy, or to check a figure against a newer scorer — had only
Developer Tools.

## The design, which keeps #1200's reasoning rather than discarding it

Two tiers, distinguished by **weight**, not by availability:

* Something IS wrong (provisional, or missing) → the prominent amber caveat
  keeps its call-to-action button.
* Nothing is wrong → a quiet footer control that states its own cost
  (*"costs 1 oracle solve"*).

That is the difference between *inviting* a household to spend oracle solves and
letting one who has already decided to do so proceed without Developer Tools.

## Both are gated on the service actually accepting the date

`_async_handle_rescore_history()` raises `ServiceValidationError` unless the date
is 1..30 days back — a day still in progress has no full-day score, and 30 is
the service's own MILP ceiling. A button that can only fail is worse than no
button, so every affordance is gated on `_isRescorable()`.

## Approach

Same zero-new-infra posture as this directory's other tests: no browser, no Node
in CI, so these are structural checks on the JS source.

**The logic was verified separately in real Node**, by extracting the actual
method bodies from the card source — not a transcribed copy — and exercising
them across ten cases: a present row, an absent row, null attrs, attrs with no
history key, and `_isRescorable` at 0 / 1 / 10 / 30 / 31 days back plus a
malformed date. All ten behaved as intended, including both boundaries.

That run also caught a bug — in the harness, not the card: formatting dates with
`toISOString()` is UTC and shifted every date by one day on a UTC+7 machine,
which made the 0-day and 30-day cases look wrong. Worth recording, because the
card's own `_formatLocalDate()` exists for exactly that reason.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CARD_JS = (
    REPO_ROOT
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-regret-card.js"
)


def _source() -> str:
    return CARD_JS.read_text(encoding="utf-8")


def _method(name: str) -> str:
    """One method's own source, brace-matched, so an assertion below cannot
    accidentally match some other part of the file."""
    src = _source()
    i = src.index(f"  {name}(")
    open_brace = src.index("{", i)
    depth = 0
    for j in range(open_brace, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _render() -> str:
    return _method("_render")


class TestTheMissingRowCaseIsOffered:
    """#1248's case: the row is absent, so the figures are a live estimate."""

    def test_the_predicate_exists(self):
        assert "_missingFromTable(dateKey, attrs)" in _source()

    def test_it_reports_missing_only_when_the_table_is_readable(self):
        """ "I cannot see the table" is not "the row is missing".

        Offering a repair on the strength of a failed read would spend an
        oracle solve to fix nothing, so an absent entity or absent history
        must return false rather than true.
        """
        body = _method("_missingFromTable")
        assert "if (!attrs || !attrs.history) return false;" in body

    def test_the_render_consults_it(self):
        assert "this._missingFromTable(" in _render()

    def test_it_offers_a_button_with_its_own_wording(self):
        """Not the settlement wording -- a missing row is a different problem
        and "Re-score with settlement" would misdescribe it."""
        render = _render()
        assert "Not in the published table" in render
        assert "Score this day into the table" in render

    def test_the_missing_block_does_not_double_up_with_provisional(self):
        """A row can be both absent and (by the headline) provisional. Two
        stacked amber blocks with two buttons of the same id would be a
        duplicate-id DOM bug, so missing defers to provisional."""
        assert "missing && !provisional" in _render()


class TestTheOnDemandControlExists:
    """The gap that had no coverage at all: re-scoring a day that is fine."""

    def test_there_is_a_footer_button(self):
        render = _render()
        assert 'id="onDemandBtn"' in render
        assert "Re-score this day" in render

    def test_it_states_its_own_cost(self):
        """#1200's concern was households buying oracle solves unawares. The
        answer is to say the price, not to hide the control."""
        assert "costs 1 oracle solve" in _render()

    def test_it_appears_only_when_nothing_is_wrong(self):
        """When something IS wrong the prominent caveat button is the right
        affordance; showing both would offer the same action twice."""
        assert "rescorable && !provisional && !missing" in _render()

    def test_it_is_styled_more_quietly_than_the_caveat_button(self):
        """The distinction #1200's reasoning survives on is weight. If this
        rendered as another amber call-to-action, the gate would effectively
        have been deleted rather than refined."""
        src = _source()
        assert ".ondemand {" in src
        assert ".ondemand .cost {" in src
        # The caveat's own amber border must NOT be applied to it.
        i = src.index(".ondemand {")
        block = src[i : src.index("}", i)]
        assert "#E8A33D" not in block


class TestEveryAffordanceIsGatedOnAnAcceptableDate:
    """A button that can only fail is worse than no button."""

    def test_the_predicate_exists(self):
        assert "_isRescorable(dateKey)" in _source()

    def test_it_enforces_the_services_own_one_to_thirty_day_range(self):
        body = _method("_isRescorable")
        assert "back >= 1 && back <= 30" in body

    def test_it_rejects_a_malformed_date_rather_than_computing_on_NaN(self):
        assert "Number.isNaN(target.getTime())" in _method("_isRescorable")

    def test_all_three_affordances_check_it(self):
        render = _render()
        assert "provisional && rescorable" in render
        assert "missing && !provisional && rescorable" in render
        assert "rescorable && !provisional && !missing" in render


class TestTheClickHandlerIsSharedAndStillCostsOneSolve:
    """#1208's fix must survive being reused by a second button."""

    def test_the_handler_is_bound_in_one_place(self):
        assert "_bindRescore(buttonId, msgId)" in _source()

    def test_both_buttons_are_bound(self):
        render = _render()
        assert '_bindRescore("repairBtn", "repairMsg")' in render
        assert '_bindRescore("onDemandBtn", "onDemandMsg")' in render

    def test_it_asks_for_a_DATE_not_a_lookback(self):
        """The whole of #1208. `days: N` re-scores every day from today back to
        N, so repairing a ten-day-old row cost ten oracle MILPs in one
        synchronous call -- the executor-starvation shape the automatic sweep is
        gated against, re-entering through a button."""
        body = _method("_bindRescore")
        # Asserted on the PAYLOAD, not the whole method: the first version of
        # this test failed on the comment that quotes #1208's own defect
        # ("`days: N` re-scores every day from today back to N"), which is
        # documentation of the bug, not the bug.
        start = body.index('callService("nimbus_load", "rescore_history"')
        payload = body[start : body.index(")", body.index("}", start))]
        assert "date: dateKey," in payload
        assert "days" not in payload

    def test_it_drops_the_cache_so_the_rewritten_row_is_read(self):
        """Without this the card re-renders the figures the click was
        complaining about, and the button looks broken."""
        assert "this._cache = null;" in _method("_bindRescore")

    def test_a_failure_re_enables_the_button_and_says_why(self):
        """A disabled button with no message is indistinguishable from a
        hung one."""
        body = _method("_bindRescore")
        assert "btn.disabled = false;" in body
        assert "failed: " in body

    def test_binding_a_button_that_is_not_rendered_is_safe(self):
        """Exactly one of the two exists per render, so the other id is always
        absent -- an early return rather than a thrown listener attach."""
        assert "if (!btn) return;" in _method("_bindRescore")
