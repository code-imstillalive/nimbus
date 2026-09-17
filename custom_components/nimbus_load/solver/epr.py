"""Economic Performance Ratio (EPR) -- the CER-industry-standard-shaped
metric this domain has never actually had, per Mark Purcell's own
framing (2026-08-16): "Solar already solved this. A PV plant doesn't
report irradiance regret, it reports a performance ratio: actual yield
over theoretical yield. Do the same for CER economics."

    EPR = (J_ref - J_ach) / (J_ref - J*)

Same maths as regret.py's own R = J_ach - J*, and counterfactuals.py's
own closed_fraction -- per Mark's own point, nothing in the analysis
changes, only the direction and vocabulary of what's reported.
regret.py stays the engine (still computes the real R, still the right
quantity for the bootstrap-over-days statistics his own test #1 asks
for); this file is the naming/reporting layer -- what actually gets
shown to a person.

An earlier version of this reporting layer (value_capture.py, same
session) used a plain "% captured" framing with j_no_control as a
hardcoded reference. Superseded by this file per Mark's direct
follow-up: EPR is bounded, dimensionless, and comparable across
sites/seasons in a way a percentage-against-one-fixed-baseline is not,
and the vocabulary swap below is the more complete answer to "reframe
positively" than a single renamed number was.

## Vocabulary (Mark's own glossary, 2026-08-16)

Use these terms in any future reporting -- dashboards, logs, PR
descriptions -- not the regret-flavored equivalents on the right:

    value headroom / uplift available     <- regret
    theoretical maximum yield             <- perfect foresight optimum (J*)
    where the next dollar is, by layer    <- regret by layer
    peak capture rate                     <- missed evening export
    service assurance rate                <- ran out of hot water
    absorption capture                    <- missed negative-price charge
    window utilisation                    <- stranded energy at close

## Why this is technically better, not just friendlier (Mark's own
three reasons, kept here so the rationale travels with the code)

- Has a ceiling. MAE has no interpretable target, so "better" is
  unbounded. EPR tops out at 1.0, which lets a component be declared
  done and moved off the list.
- Comparable and publishable. "EPR 0.91 on flat-price P2P vs 0.78 on
  spot exposure" is a reproducible result. Regret in $/day is
  site-specific and isn't.
- Turns named failure scenarios into capability gaps, not mistakes.
  "Absorption capture is 40% because the battery fills by 13:00" starts
  a design conversation. "You missed the negative window" starts an
  argument.

## Broader context (Mark, same message)

"There is no standard economic performance metric for CER. Solar has
PR, wind has capacity factor, batteries have round-trip efficiency, and
household optimisers have vendor savings claims. An EPR against a
perfect-information benchmark, with a published decomposition across
topology, forecasting, optimisation and control, is worth more than a
Discord thread." The per-layer decomposition itself (topology /
forecasting / optimisation / control, per Mark's own closing point on
his original 9-item review) is not built yet -- this file is the
reporting shape it should land in once it exists, not the decomposition
itself.

## Scope note (2026-08-29): what "performance" means here

EPR is a WHOLE-HOUSEHOLD economic-dispatch ratio, not a battery-only
one. J_star (oracle) and J_ach (real) are both scored on network.py's
full LP, which co-optimises every economic degree of freedom the
household actually has:

- battery charge / discharge -- the obvious one,
- solar_curtailed_kw -- a real LP decision variable, chosen freely by
  the oracle (e.g. curtailing to avoid negative export prices, or
  holding energy back to import instead when an import bonus beats
  exporting),
- shed_kw per sheddable load -- deferrable/reducible loads within
  their min_fraction bounds, each priced with a real shed_cost in the
  objective.

J_ref is the "battery physically disabled" baseline (see
counterfactuals.py's no_control_dispatch()), so the ratio's numerator
J_ref - J_ach captures the dollar uplift from ANY of those levers,
not just battery arbitrage; the denominator J_ref - J_star is the
total uplift genuinely available across the same lever set.

This is deliberately wider scope than a BESS-industry "capture rate",
and the distinction matters for external framing:

- Battery capture rate (NEMPulse's own NEM benchmark, Modo Energy's
  ERCOT TBx-based one) scores actual battery revenue against a
  perfect-foresight OR simple top-minus-bottom-spread benchmark that
  models the battery as a standalone asset -- there is no solar to
  curtail and no load to shed in the denominator. The RATIO SHAPE is
  identical to EPR (actual dollars over theoretical dollars), which
  is why a BESS-audience reader will recognise EPR immediately, but
  the SCOPE is narrower.
- Wind/solar "capture rate" (a.k.a. value factor, Marktwertfaktor)
  is a completely different animal -- volume-weighted-price divided
  by time-weighted-baseload-price, no benchmark trajectory at all,
  and a cannibalisation-vs-market-average measure rather than an
  optimisation-performance one. NOT interchangeable with EPR under
  any framing.
- Solar's Performance Ratio (actual kWh yield / theoretical kWh yield
  under measured irradiance) is the SHAPE inspiration for EPR -- see
  the top of this docstring -- but denominated in dollars against a
  perfect-foresight economic optimum, not kWh against irradiance.

So: when writing for a battery-market audience, call it EPR and, if
the capture-rate analogy is worth drawing, be explicit that the scope
is wider (battery + solar curtailment + load shed co-optimised, not
just battery arbitrage). When writing for a wind/solar audience, call
it EPR and reach for solar PR as the analogy, not "capture rate" (the
value-factor collision above will actively mislead them). In mixed or
unknown audiences, EPR alone -- with a one-line reminder of the (J_ref
- J_ach) / (J_ref - J_star) formula -- is the safe framing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EPRResult:
    """`epr` is the headline number -- everything else is supporting
    detail for an opportunity backlog, not the first thing reported.
    """

    epr: float  # dimensionless, 0.0-1.0 in the normal case (see compute_epr()'s own note on what a value outside that range would mean)
    theoretical_maximum_yield: (
        float  # $, j_ref - j_star -- the total value genuinely available in this window
    )
    value_captured: float  # $, j_ref - j_ach -- what was actually captured
    uplift_available: float  # $, j_ach - j_star -- same quantity regret.py calls R; kept as supporting detail, never the headline
    # nimbus issue #1089: None when `theoretical_maximum_yield` is a
    # positive quantity (the normal case, and what reading `epr` as a
    # percentage assumes). A reason string when it is not, in which case
    # `epr` is a ratio of two signed dollar amounts rather than a
    # fraction of anything available -- see compute_epr()'s own
    # docstring for the real 242.7% this was measured on.
    #
    # Carried on the result rather than computed by each caller so that
    # every consumer of an EPR gets the check for free: the native
    # integration, the standalone cron writer, and anything built later.
    # Defaulted so an EPRResult constructed positionally in an existing
    # test keeps working.
    denominator_reason: str | None = None


def compute_epr(*, j_ref: float, j_ach: float, j_star: float) -> EPRResult:
    """Mark Purcell's EPR (2026-08-16): (j_ref - j_ach) / (j_ref - j_star).

    j_ref: the reference baseline this ratio is measured against -- NOT
    hardcoded to one counterfactual. Pass counterfactuals.py's own
    no_control_dispatch() result for the standard "vs doing nothing at
    all" EPR (the closest analogue to solar PR's own theoretical-yield
    reference), or tune_two_threshold()'s own result for a "vs a simple
    human rule" EPR -- both are legitimate, differently-scoped
    questions. Whichever is chosen must be held CONSTANT across any set
    of EPR values being compared to each other (Mark's own "comparable
    across sites and seasons" claim only holds if the reference doesn't
    silently change between the numbers being compared).
    j_ach: regret.py's own evaluate_realized_cost() result for whatever
    trajectory is being scored (a real committed dispatch, for a real
    reconciliation; a rolling-refinement result, for evaluating NIMBUS
    itself).
    j_star: regret.py's own oracle_dispatch() result for the SAME real
    window (perfect foresight -- "theoretical maximum yield").

    A value outside [0, 1] is informative, not an error: epr < 0 means
    the scored trajectory did WORSE than the reference baseline (a real
    possibility -- e.g. a genuinely bad forecast driving a worse-than-
    doing-nothing decision).

    **epr > 1 has TWO mechanisms, and this docstring named only one
    until 2026-09-18.** It used to say epr > 1 "would mean j_ach beat
    j_star" -- i.e. look for a negative regret. That is one route, and
    it is the route #956 investigated. The other is a NEGATIVE
    DENOMINATOR, and it is the more dangerous of the two because the
    sign cancels:

        EPR = (j_ref - j_ach) / (j_ref - j_star)

    If the scored trajectory did worse than the baseline (numerator
    negative) AND j_star is no better than j_ref (denominator negative),
    the quotient is POSITIVE. A bad day then publishes as a high score.

    Measured on a real install, 2026-09-17:

        j_ref   5.4897      numerator    j_ref - j_ach = -3.4975
        j_ach   8.9872      denominator  j_ref - j_star = -1.4411
        j_star  6.9308      EPR = 2.427  ->  published as 242.7%

    Regret on that day was +2.0564 -- POSITIVE, so every
    regret-based reliability signal read clean. A reader following this
    docstring's old advice would check regret, find nothing wrong, and
    accept 242.7% on a day the household spent $3.50 MORE than leaving
    the battery alone.

    `j_star > j_ref` is itself structurally impossible when both are
    priced by the same model -- the oracle may always choose to do
    nothing, so it can never be beaten by doing nothing. It is therefore
    always evidence about the COMPUTATION rather than about the day, and
    on this install it is #1081's finding: j_star is the LP's own
    objective (carrying soft-SoC and slack penalties) while j_ref comes
    from the independent evaluator, so the two sides are not priced
    alike. Re-pricing the same oracle plan through the evaluator gave
    4.4419 against the LP's 6.9308, restoring a positive denominator
    (+1.0478) and an honest EPR of -3.34.

    So: epr > 1 is worth investigating as a likely bug rather than
    reported as a real result, and the FIRST thing to check is the sign
    of `theoretical_maximum_yield`, not the sign of regret.
    `solver_writer.py`'s own `_epr_denominator_reason()` performs that
    check on every published report.

    Degenerate case: if j_ref == j_star (no real opportunity existed in
    this window -- e.g. genuinely flat prices, nothing to arbitrage),
    epr is reported as 1.0 (nothing available, nothing missed) rather
    than dividing by zero -- there is no headroom to have missed.
    """
    theoretical_maximum_yield = j_ref - j_star
    value_captured = j_ref - j_ach
    if abs(theoretical_maximum_yield) < 1e-9:
        epr = 1.0
    else:
        epr = value_captured / theoretical_maximum_yield
    return EPRResult(
        epr=epr,
        theoretical_maximum_yield=theoretical_maximum_yield,
        value_captured=value_captured,
        uplift_available=j_ach - j_star,
        denominator_reason=denominator_reason(j_ref=j_ref, j_star=j_star),
    )


def denominator_reason(*, j_ref: float, j_star: float) -> str | None:
    """Whether EPR's denominator is a positive quantity, which every
    reading of `epr` as a percentage silently assumes (nimbus #1089).

    `j_star <= j_ref` holds BY CONSTRUCTION when both are priced by the
    same model: the oracle may always choose to do nothing, so the idle
    baseline is inside its feasible set and it can never be beaten by
    doing nothing. A violation is therefore never a property of the day
    -- it is always evidence that the two sides were not priced alike.

    On the reference install that cause is nimbus #1081: `j_star` is the
    LP's own objective and carries soft-SoC and slack penalties that the
    independent evaluator producing `j_ref` does not. Re-pricing the
    same oracle plan through the evaluator turned a -1.4411 denominator
    into +1.0478. This function deliberately does not try to repair the
    number: which side should move rescores every historical day, and
    that is #1081's decision to make.

    Deliberately a SIGN test with no tolerance. A denominator that is
    merely SMALL also makes `epr` volatile, but choosing where "small"
    begins needs a measured basis that does not exist yet, and inventing
    a threshold would be the guess this check exists to replace. The
    magnitude is already published as `theoretical_maximum_yield` for a
    reader who wants it, and `compute_epr()` special-cases
    `abs(denominator) < 1e-9` to 1.0 so the exact-zero case never
    reaches a division.
    """
    if j_star > j_ref:
        return "oracle_not_better_than_idle"
    return None
