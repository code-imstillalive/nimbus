"""Regression test for nimbus issue #351 (Mark Purcell, codebase review),
the GBRT early-stopping leakage half of the finding: train_model()'s
model-selection GBRT candidate used to early-stop against x_val/y_val --
the SAME points validation_mae["gbrt"] was then computed from. GBRT gets
to pick whichever boosting round scores best on that exact set, then
reports that same set's error at that exact round as its accuracy -- an
optimistic bias neither the k-NN candidate (no per-round tuning) nor the
naive baseline (no tuning at all) receive.

Fixed by early-stopping against a further chronological split of the
TRAINING portion instead, leaving x_val/y_val genuinely untouched until
the final MAE computation.

This test proves the two are disjoint by construction: the model-
selection GBRT's own early-stopping y_val (captured by patching
GBRT.fit()) must share zero values with the y_val the final MAE
comparison is computed from (captured by patching model._mae) -- the
synthetic series' own drift term guarantees every real value in it is
unique, so any overlap would mean the same rows were used for both
roles.

Real, from-scratch synthetic data through the actual train_model() --
same "ml/model.py has zero homeassistant.* imports, directly
importable" convention as this project's other ml test files.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _ml_path  # noqa: F401
from nimbus_load.ml import gbrt as gbrt_module
from nimbus_load.ml import model as ml_model

START_OF_TIME = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001


def _hourly_load_events(days: float) -> list[tuple[datetime, float]]:
    """Deliberately NOT the sine-plus-drift generator this project's
    other ml test files use: a periodic component combined with a
    linear drift can produce genuine floating-point VALUE collisions
    between two widely-separated indices whenever the sine term happens
    to land on a "nice" fraction (confirmed while writing this test --
    a real coincidence, not a bug in the fix under test). A strictly
    monotonic, non-periodic sequence guarantees every value is globally
    unique, which is what this test's own overlap check depends on to
    mean anything."""
    n_hours = int(days * 24)
    events = []
    for i in range(n_hours):
        t = START_OF_TIME + timedelta(hours=i)
        value = 3.0 + 0.013 * i
        events.append((t, value))
    return events


class TestGbrtEarlyStoppingNoValidationLeakage(unittest.TestCase):
    def test_model_selection_early_stopping_split_never_overlaps_the_final_mae_split(
        self,
    ):
        days = 20.0
        start = START_OF_TIME
        end = start + timedelta(days=days)
        load_events = _hourly_load_events(days)

        real_gbrt_fit = gbrt_module.GBRT.fit
        early_stopping_y_vals: list[tuple[float, ...]] = []

        def spying_fit(self, x, y, *, x_val=None, y_val=None, **kwargs):
            if y_val is not None:
                early_stopping_y_vals.append(tuple(float(v) for v in y_val))
            return real_gbrt_fit(self, x, y, x_val=x_val, y_val=y_val, **kwargs)

        real_mae = ml_model._mae
        final_mae_y_vals: list[tuple[float, ...]] = []

        def spying_mae(y_true, y_pred):
            final_mae_y_vals.append(tuple(float(v) for v in y_true))
            return real_mae(y_true, y_pred)

        with (
            patch.object(gbrt_module.GBRT, "fit", spying_fit),
            patch.object(ml_model, "_mae", spying_mae),
        ):
            trained = ml_model.train_model(
                load_events=load_events,
                temp_events=[],
                humidity_events=[],
                curtailment_events=[],
                start=start,
                end=end,
                resample_minutes=60,
                min_training_points=50,
            )

        self.assertIsNotNone(trained)
        self.assertGreater(
            len(early_stopping_y_vals),
            0,
            "expected at least one GBRT.fit() call to use early stopping "
            "-- test isn't exercising the code path it's meant to guard",
        )
        self.assertGreater(len(final_mae_y_vals), 0)

        # The final MAE comparison's own y_val (identical across knn/
        # gbrt/naive -- capture the first, they're all the same object)
        final_y_val = set(final_mae_y_vals[0])

        # Only the FIRST fit() call is the model-selection candidate
        # (gbrt_val) this issue is about. A later fit() call with early
        # stopping (the genuine quantile models, gbrt_lower_final/
        # gbrt_upper_final, fit only when GBRT wins model selection)
        # legitimately DOES use the real held-out split for ITS OWN
        # early stopping -- nothing downstream computes an accuracy
        # metric from those against y_val, so that's not leakage and
        # must not be flagged here.
        model_selection_es_y_val = early_stopping_y_vals[0]
        overlap = final_y_val & set(model_selection_es_y_val)
        self.assertEqual(
            overlap,
            set(),
            f"the model-selection GBRT candidate's own early-stopping "
            f"y_val shares {len(overlap)} value(s) with the final MAE "
            "comparison's held-out y_val -- this is the leakage nimbus "
            "issue #351 describes (early stopping peeking at the set "
            "it's later judged against)",
        )


if __name__ == "__main__":
    unittest.main()
