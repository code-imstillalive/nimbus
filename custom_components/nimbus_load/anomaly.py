"""Cross-signal / self-diagnosis anomaly detection (2026-08-25, direct
household ask: "is there anything... we could add ot make the whole
thing next level smart", narrowed via "the second one interests me
more" to a shared anomaly layer over auto-tuning).

Real motivation, grounded in this project's own bug history rather than
a generic idea: looking back at Mark Purcell's own bug reports (#105
phantom load, #107 solar-export cap, #114 invisible curtailment, #148
wrong load source silently used), nearly all of them share the same
shape -- a real data-quality or config problem that only got caught
because a human stared at a live chart and noticed something looked
wrong. This module's job is to catch that same shape of problem
automatically, continuously, without needing a human watching.

Deliberately pure numpy/stdlib, zero Home Assistant dependencies,
matching every other module in this package (ml/blend.py, ml/model.py)
-- these functions are plain math, testable and reusable independent of
where a caller wires them in.

STRICTLY OBSERVATIONAL BY DESIGN: every function here takes real data
in and returns an anomaly record (or None) out -- nothing in this
module ever raises on bad input in a way that could interrupt a solve
or a forecast cycle, and nothing here has any path back into the
Solver's own dispatch decisions (network.py/elements.py never import
this module). A caller's own job is to log whatever this module
returns; that's the full extent of this module's blast radius. This is
the direct answer to "only if it doesn't kill overall function" -- it
structurally cannot.

Every threshold here is SELF-calibrated from a signal's own recent
history, never a fixed global constant -- same discipline already
proven in solver_writer.py's own apply_price_band()/compute_price_
percentile_band(), and the direct lesson of coordinator.py's own
confidence-band clamp-to-observed-range fix (bug #5 in this project's
own documented history: an unbounded fixed-shape band grew to a real,
confirmed +100kW absurdity before being clamped to each signal's own
real training range).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResidualDriftAnomaly:
    """A signal's recent one-step-ahead forecast error is significantly
    worse than that SAME signal's own historical baseline -- the model
    has degraded, the underlying sensor has drifted, or new curtailment/
    contamination is corrupting recent readings. Which of these it is
    isn't this module's job to diagnose further; naming that the
    residual itself has drifted is already the useful signal, matching
    this project's own "diagnostic exposure, not automatic remediation"
    convention for genuinely ambiguous root causes.
    """

    recent_mean_error: float
    baseline_mean_error: float
    ratio: float


def detect_residual_drift(
    residuals: list[float],
    *,
    min_history: int = 20,
    recent_window: int = 10,
    drift_multiplier: float = 2.0,
) -> ResidualDriftAnomaly | None:
    """Compares the mean of the most recent `recent_window` one-step-
    ahead residuals against the mean of everything BEFORE that window
    (the signal's own recent baseline) -- flags drift only when the
    recent mean exceeds the baseline mean by `drift_multiplier`.

    Deliberately reuses `residuals` as already maintained by
    NimbusCoordinator (`self._residuals`, the same rolling |predicted -
    actual| list already feeding ml/model.py's own calibrated_band()) --
    this function adds no new data collection, only a second, cheap
    analysis pass over data a coordinator already computes every cycle.

    `min_history` (need enough total residuals for the comparison to be
    meaningful at all) and requiring a FULL `recent_window` (not a
    single noisy blip) are the alert-fatigue guardrails -- a signal that
    just started accumulating residuals, or had one unlucky miss, must
    not flag. Returns None whenever there isn't enough history, or the
    baseline itself is ~zero (a division-by-a-near-perfect-baseline
    would produce a meaningless, wildly oversensitive ratio).
    """
    if len(residuals) < min_history or len(residuals) <= recent_window:
        return None
    recent = residuals[-recent_window:]
    baseline = residuals[:-recent_window]
    if not baseline:
        return None
    recent_mean = sum(recent) / len(recent)
    baseline_mean = sum(baseline) / len(baseline)
    if baseline_mean <= 1e-9:
        return None
    ratio = recent_mean / baseline_mean
    if ratio <= drift_multiplier:
        return None
    return ResidualDriftAnomaly(
        recent_mean_error=recent_mean,
        baseline_mean_error=baseline_mean,
        ratio=ratio,
    )
