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
    doing-nothing decision); epr > 1 would mean j_ach beat j_star, which
    should not happen for a correctly-computed oracle over the identical
    window and real data, and would itself be worth investigating as a
    likely bug (mismatched windows, non-comparable inputs) rather than
    reported as a real result.

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
    )
