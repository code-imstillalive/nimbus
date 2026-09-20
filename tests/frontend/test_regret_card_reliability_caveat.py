"""nimbus #1162: the Regret card rendered an EPR the report had already
declared unreliable, and said nothing.

The household's report was that EPR "is seriously not working well" --
readings of 46-54% that looked like nonsense. Underneath, the quality
report had been publishing `epr_reliable: false` for those same days.
The card never read it: before this change the only attribute it touched
was `attributes.history`, and the reliability verdict is not in there.

So the data said "do not trust this number" and the surface a household
actually looks at showed the number alone.

## What it now shows

Driven with the reference household's own 19 Sep attributes:

    This day's EPR is not a reliable measurement: the reconstructed
    battery SoC disagrees with the real sensor (disagreement).
    90% of this regret is the two pricing paths disagreeing about the
    oracle's own plan, not the household having dispatched differently.

The second line is #1162 ask 3 reaching the surface: `j_star_path_delta`
is exactly the portion of the regret that comes from which oracle price
is used, and on that day it was most of it.

## The gate that matters most

The reliability attributes are **top-level and describe `latest_date`
only**, while this card can be pointed at any scored day. Showing the
latest day's caveat above an older day's figures would be the same
cross-day confusion #1167 was about, one layer up -- a caveat and the
figures it qualifies describing different days.

`_caveatFor()` returns null unless `attrs.latest_date === dateKey`, and
`TestItIsGatedOnTheDisplayedDay` below is the load-bearing test here.

## Approach

Same "zero new infra" posture as this directory's existing tests: no
browser, no Node in CI (confirmed -- there is no Node setup anywhere in
.github/workflows/ci.yml), so these are structural checks on the JS
source.

The LOGIC was verified separately with real Node before these assertions
were written, by evaluating the actual card module with DOM stubs and
calling the real `_caveatFor()` -- not a transcribed copy -- across seven
scenarios: the real production attributes, an older day displayed, a
healthy day, `oracle_beaten`, `achieved_soc_unverifiable`, a reliable day
whose delta dominates, and null attributes. Each produced the intended
output, including null for both cases that must stay silent.
"""

from __future__ import annotations

import pathlib
import re

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


def _caveat_fn() -> str:
    """Just the `_caveatFor` method body, so assertions below cannot
    accidentally match some other part of the file."""
    src = _source()
    start = src.index("_caveatFor(dateKey, attrs) {")
    end = src.index("\n  _escape(s) {", start)
    return src[start:end]


class TestItIsGatedOnTheDisplayedDay:
    """The load-bearing test.

    Reliability attributes describe `latest_date`. This card can show any
    scored day. Losing the gate would put the latest day's caveat above
    an older day's figures -- a caveat and the figures it qualifies
    describing different days, which is #1167 one layer up.
    """

    def test_the_gate_exists(self):
        assert "attrs.latest_date !== dateKey" in _caveat_fn(), (
            "the caveat is no longer gated on the displayed date, so it can "
            "attribute the latest day's verdict to a different day"
        )

    def test_the_gate_returns_nothing_rather_than_falling_through(self):
        fn = _caveat_fn()
        gate = fn[fn.index("attrs.latest_date !== dateKey") :][:60]
        assert "return null" in gate

    def test_missing_attributes_are_survivable(self):
        assert "!attrs" in _caveat_fn()


class TestItReadsTheReliabilityVerdict:
    def test_it_reads_epr_reliable(self):
        assert "attrs.epr_reliable === false" in _caveat_fn(), (
            "the card is back to rendering an EPR without checking whether "
            "the report stands behind it"
        )

    def test_every_published_reason_shape_is_handled(self):
        fn = _caveat_fn()
        for reason in (
            "achieved_soc_unreliable",
            "achieved_soc_unverifiable",
            "oracle_beaten",
            "epr_denominator_reason",
        ):
            assert reason in fn, (
                f"{reason!r} is a value the report publishes and the card has "
                "no sentence for it, so a household sees a bare code"
            )

    def test_an_unrecognised_reason_still_says_something(self):
        """A reason the card has no sentence for must still surface, not
        vanish -- the absence-is-the-only-signal failure this repo keeps
        recording."""
        fn = _caveat_fn()
        assert 'reason || "the report did not say why"' in fn


class TestThePathDeltaShareReachesTheSurface:
    """#1162 ask 3. A regret that is mostly a pricing-path disagreement
    reads as a dispatch finding, and sends a household looking for a
    mistake that is mostly not there."""

    def test_it_reads_the_share(self):
        assert "attrs.regret_path_delta_share" in _caveat_fn()

    def test_it_only_speaks_when_the_share_dominates(self):
        fn = _caveat_fn()
        assert "share >= 0.5" in fn, (
            "either the threshold is gone -- so the note fires on every day "
            "including ones where the delta is irrelevant -- or it moved "
            "without this test being reconsidered"
        )

    def test_a_non_numeric_share_is_ignored(self):
        assert "Number.isFinite(share)" in _caveat_fn()


class TestItIsActuallyRendered:
    """A correct helper nothing calls is the failure shape today's own
    releases kept taking."""

    def test_the_render_path_builds_and_emits_it(self):
        src = _source()
        assert "this._caveatFor(" in src, "the helper is never called"
        body = src[src.index("bodyEl.innerHTML = `\n      ${caveat") :][:200]
        assert 'class="caveat"' in body, (
            "the caveat is computed but not emitted into the card body"
        )

    def test_it_is_emitted_above_the_stats(self):
        """A caveat under the number it qualifies is a footnote. Above it,
        it is a caveat."""
        body = _source()
        i_caveat = body.index('class="caveat"')
        i_stats = body.index('<div class="stats">')
        assert i_caveat < i_stats

    def test_it_renders_nothing_when_there_is_no_caveat(self):
        """A healthy day must not gain an empty box."""
        src = _source()
        assert re.search(r"\$\{caveat \? `<div class=\"caveat\">.*?` : \"\"\}", src), (
            "the caveat element is emitted unconditionally, so a clean day "
            "shows an empty amber box"
        )

    def test_the_style_exists(self):
        assert ".caveat {" in _source()


class TestTheCapacityDiagnosis:
    """nimbus #1172 on the surface: "disagreement" cannot say WHICH
    disagreement, and a household reading the caveat had no way to tell a
    mis-set capacity from a fleet blend comparing different things."""

    def test_it_reads_both_halves_of_the_pair(self):
        fn = _caveat_fn()
        assert "attrs.measured_usable_capacity_kwh" in fn
        assert "attrs.configured_usable_capacity_kwh" in fn

    def test_it_only_speaks_when_the_soc_half_is_what_fired(self):
        """A day unreliable for `oracle_beaten` has nothing to do with
        capacity, and saying so there would be noise."""
        assert 'startsWith("achieved_soc")' in _caveat_fn()

    def test_it_has_a_materiality_floor(self):
        """A 1% difference is measurement resolution, not a finding."""
        assert "Math.abs(offPct) >= 5" in _caveat_fn()

    def test_a_missing_measurement_is_survivable(self):
        """`measured_usable_capacity_kwh` is null on most days -- the
        shallow-cycle case -- and must not produce a NaN sentence."""
        fn = _caveat_fn()
        assert "Number.isFinite(measured)" in fn
        assert "Number.isFinite(configured)" in fn

    def test_it_states_the_comparison_rather_than_asserting_a_cause(self):
        """One install's arithmetic closing is not licence to claim
        causation generally."""
        fn = _caveat_fn()
        assert "would account for" in fn
        assert "roughly" in fn


class TestTextIsEscaped:
    """The reason string comes from the report, and the card builds HTML
    by concatenation."""

    def test_the_reason_is_escaped(self):
        assert "this._escape(why)" in _caveat_fn()
