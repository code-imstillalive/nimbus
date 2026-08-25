"""Real test of detect_residual_drift() (anomaly.py).

First check in the new cross-signal anomaly-detection layer (2026-08-25,
direct household ask: "is there anything... we could add ot make the
whole thing next level smart", narrowed to "the second one interests me
more" -- a shared anomaly/self-diagnosis layer over closed-loop auto-
tuning, built "only if well engineered and if it doesnt kill overall
function").

Pure stdlib module, zero HA imports, imported directly like
ml/blend.py's own tests -- no stub harness needed.
"""

import _solver_path  # noqa: F401 -- adds custom_components/nimbus_load to sys.path
from anomaly import ResidualDriftAnomaly, detect_residual_drift


def test_stable_residuals_never_flag():
    # A signal performing consistently (recent errors similar to its own
    # historical baseline) must never flag -- this is the overwhelming
    # majority, steady-state case.
    residuals = [1.0, 1.1, 0.9, 1.0, 1.2, 0.8, 1.0, 1.1, 0.9, 1.0] * 3
    assert detect_residual_drift(residuals) is None


def test_insufficient_history_never_flags_even_with_huge_recent_errors():
    # Guardrail: not enough total history for the comparison to be
    # meaningful, regardless of how bad recent numbers look.
    residuals = [1.0] * 5 + [100.0] * 5
    assert detect_residual_drift(residuals, min_history=20) is None


def test_clear_drift_is_detected():
    # A real degradation shape: a long, stable baseline followed by a
    # recent window that's dramatically worse.
    baseline = [1.0, 1.1, 0.9, 1.0, 1.2, 0.8, 1.0, 1.1, 0.9, 1.0] * 2
    recent = [5.0] * 10
    residuals = baseline + recent
    anomaly = detect_residual_drift(residuals, min_history=20, recent_window=10)
    assert isinstance(anomaly, ResidualDriftAnomaly)
    assert anomaly.ratio > 2.0
    assert round(anomaly.baseline_mean_error, 3) == 1.0
    assert round(anomaly.recent_mean_error, 3) == 5.0


def test_mild_variation_below_the_multiplier_does_not_flag():
    # Real day-to-day noise (recent errors somewhat higher, but not
    # dramatically so) must not trip the default 2x multiplier -- this
    # is the actual alert-fatigue guardrail being exercised, not just
    # documented.
    baseline = [1.0] * 10
    recent = [1.5] * 10  # 1.5x, below the default 2.0x threshold
    residuals = baseline + recent
    assert detect_residual_drift(residuals, min_history=20, recent_window=10) is None


def test_single_noisy_blip_within_recent_window_does_not_dominate():
    # One bad point inside an otherwise-normal recent window shouldn't
    # necessarily trip drift on its own -- the recent MEAN across the
    # whole window is what matters, not any single value.
    baseline = [1.0] * 10
    recent = [1.0] * 9 + [3.0]  # one outlier, mean still close to baseline
    residuals = baseline + recent
    assert detect_residual_drift(residuals, min_history=20, recent_window=10) is None


def test_near_zero_baseline_does_not_produce_a_meaningless_ratio():
    # A near-perfect baseline (essentially zero error) must not turn a
    # tiny absolute recent error into a wildly inflated, meaningless
    # ratio -- division-by-near-zero guard.
    baseline = [1e-10] * 10
    recent = [0.01] * 10
    assert detect_residual_drift(baseline + recent, min_history=20) is None


def test_exactly_at_the_multiplier_boundary_does_not_flag():
    # Strictly greater than the multiplier, not greater-or-equal --
    # documents the exact boundary behaviour.
    baseline = [1.0] * 10
    recent = [2.0] * 10  # exactly 2.0x == the default drift_multiplier
    assert detect_residual_drift(baseline + recent, min_history=20) is None


def test_custom_thresholds_are_respected():
    baseline = [1.0] * 5
    recent = [1.6] * 5
    residuals = baseline + recent
    # Default multiplier (2.0x) would not flag a 1.6x increase...
    assert detect_residual_drift(residuals, min_history=10, recent_window=5) is None
    # ...but a stricter, explicitly-configured multiplier should.
    anomaly = detect_residual_drift(
        residuals, min_history=10, recent_window=5, drift_multiplier=1.5
    )
    assert anomaly is not None
