"""Regression test for nimbus issue #351 (Mark Purcell, codebase review):
model_type selection used to be decided by ONE-STEP validation_mae, scored
with TRUE (ground-truth) lag_short/lag_long on every validation row -- a
15-minute nowcast, not the recursive, self-feeding 96 h forecast predict()
actually publishes in production (see this project's own documented
"exposure bias" bug chain in ml/model.py's own module docstring, and
CLAUDE.md's "Recursive-forecast bug chain" section: a real live forecast
started from an atypical "just finished a transition" moment converged to
a value far from the true pattern once recursion took over). Scoring
model selection with true lags never exercises that regime at all --
structurally flattering knn/gbrt (both get a real near-term lag on every
single validation row) and making naive "almost never win" regardless of
whether it would actually be the better DEPLOYED choice.

This test constructs a load with a sharp, mostly-quiet-then-spiky daily
shape (deterministic, not random) specifically chosen to separate the two
metrics: a model that looks best when judged on TRUE one-step lags can
still be the worst choice once its own predictions have to feed a multi-
hour recursive chain forward from an arbitrary, possibly-atypical real
anchor point. Confirmed empirically (see the diff/commit message, not
guessed) that this scenario produces a genuine, real disagreement between
the two metrics on real code, not a constructed coincidence.

Real, from-scratch synthetic data through the actual train_model() --
same "ml/model.py has zero homeassistant.* imports, directly importable"
convention as this project's other ml test files.
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

import _ml_path  # noqa: F401
from nimbus_load.ml import model as ml_model

START_OF_TIME = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001


def _spiky_evening_peak_events(
    days: float, start: datetime = START_OF_TIME
) -> list[tuple[datetime, float]]:
    """Mostly near-zero, with a short, sharp evening peak each day whose
    exact hour shifts deterministically every 3 days -- a true recent lag
    is a strong one-step predictor (the value a moment ago is genuinely
    informative about the value now, near a transition), but a recursive
    chain anchored away from "peak imminent" has to find its own way back
    to the true daily pattern rather than reading it straight off a
    stale-but-true lag, the same structural gap this issue is about.
    """
    n_hours = int(days * 24)
    events = []
    for i in range(n_hours):
        t = start + timedelta(hours=i)
        hour = t.hour
        day = i // 24
        peak_hour = 18 + (day % 3)
        if hour == peak_hour:
            value = 13.0
        elif hour in (peak_hour - 1, peak_hour + 1):
            value = 6.0
        else:
            value = 0.3 + 0.05 * math.sin(i * 0.7)
        events.append((t, value))
    return events


class TestModelSelectionUsesRecursiveNotOneStepMae(unittest.TestCase):
    def _train(self):
        days = 45.0
        start = START_OF_TIME
        end = start + timedelta(days=days)
        events = _spiky_evening_peak_events(days)
        return ml_model.train_model(
            load_events=events,
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=start,
            end=end,
            resample_minutes=60,
            min_training_points=50,
        )

    def test_one_step_and_recursive_metrics_genuinely_disagree_on_this_scenario(self):
        """Sanity check on the scenario itself, not the fix -- if this
        ever stops being true (e.g. after an unrelated change to
        build_features or the GBRT hyperparameters), the assertion below
        isn't actually testing anything and needs a new scenario, not a
        weakened check."""
        trained = self._train()
        self.assertIsNotNone(trained)
        self.assertTrue(trained.validation_mae)
        self.assertTrue(trained.validation_recursive_mae)
        one_step_winner = min(trained.validation_mae, key=trained.validation_mae.get)
        recursive_winner = min(
            trained.validation_recursive_mae, key=trained.validation_recursive_mae.get
        )
        self.assertNotEqual(
            one_step_winner,
            recursive_winner,
            "scenario no longer separates the two metrics -- pick a new "
            "one before trusting the assertion below",
        )

    def test_model_type_follows_the_recursive_winner_not_the_one_step_winner(self):
        trained = self._train()
        self.assertIsNotNone(trained)
        recursive_winner = min(
            trained.validation_recursive_mae, key=trained.validation_recursive_mae.get
        )
        one_step_winner = min(trained.validation_mae, key=trained.validation_mae.get)
        # The real regression guard: before this fix, model_type was
        # min(validation_mae, ...) -- exactly one_step_winner, never
        # recursive_winner, whenever the two disagree (confirmed they do,
        # for this scenario, by the sibling test above).
        self.assertEqual(trained.model_type, recursive_winner)
        self.assertNotEqual(trained.model_type, one_step_winner)
