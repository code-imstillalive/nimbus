"""nimbus issue #937 -- model selection validates 4 hours, the forecast
is used for 48, and the measured loss is scored at 24.

#937 found that naive persistence beat the ML forecaster on 11 of 14
scored days (mean **-$0.71/day**) on the reference household's own
data, and asked whether the model's own validation agrees.

The leading suspicion -- that selection runs on one-step MAE, blind to
the exposure-bias regime `predict()` actually operates in -- is **wrong
and was already fixed**: [#351](https://github.com/code-imstillalive/nimbus/issues/351)
made `validation_recursive_mae` the deciding metric, scored on each
candidate's own self-feeding lag chain.

The real gap is arithmetic:

    RECURSIVE_VALIDATION_HORIZON_STEPS = 16
    RESAMPLE_MINUTES                   = 15   ->  4 hours
    DEFAULT_FORECAST_HORIZON_HOURS     = 48
    #937's dollar figure is DAY-AHEAD          -> 24 hours

So the winner is chosen on one twelfth of the horizon it is chosen for.
That matters because recursive error does not grow at the same rate for
every candidate -- which is the entire reason #351 exists. k-NN's
prediction is a convex combination of observed `y_train` values and is
structurally bounded; GBRT is an unbounded additive sum that can drift
once the lag chain walks its feature vector out of distribution. A
ranking taken at 4 steps of drift need not survive to 96.

`validation_recursive_mae_by_horizon` scores the same candidates at 16,
48 and 96 steps and records all three. **Diagnostic only** -- nothing
reads it, and `model_type` is still decided from the 16-step metric
alone. If the ranking turns out to be stable across horizons, #937's
item 3 is a dead end; if the winner CHANGES, selection is demonstrably
measuring the wrong thing. Either way it becomes readable rather than
arguable.

These tests pin the two properties that make it trustworthy: that it is
computed at every horizon, and that it cannot influence the choice.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import _ml_path  # noqa: F401
from nimbus_load.ml import model as ml_model

RECURSIVE_VALIDATION_HORIZON_STEPS = ml_model.RECURSIVE_VALIDATION_HORIZON_STEPS
RECURSIVE_VALIDATION_DIAGNOSTIC_HORIZON_STEPS = (
    ml_model.RECURSIVE_VALIDATION_DIAGNOSTIC_HORIZON_STEPS
)
RECURSIVE_VALIDATION_DIAGNOSTIC_MAX_ORIGINS = (
    ml_model.RECURSIVE_VALIDATION_DIAGNOSTIC_MAX_ORIGINS
)
TrainedModel = ml_model.TrainedModel

_MODEL_SRC = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "ml"
    / "model.py"
).read_text(encoding="utf-8")

# const.py is HA-importable-free for this value, so read it as text
# rather than importing the whole integration.
_RESAMPLE_MINUTES = int(
    re.search(
        r"^RESAMPLE_MINUTES: Final = (\d+)",
        (
            Path(__file__).resolve().parent.parent
            / "custom_components"
            / "nimbus_load"
            / "const.py"
        ).read_text(encoding="utf-8"),
        re.MULTILINE,
    ).group(1)
)


class TestTheHorizonGapIsRealAndStillOpen(unittest.TestCase):
    """The arithmetic #937 rests on. If any of these change, the issue's
    own framing needs re-reading rather than the test relaxing."""

    def test_selection_validates_four_hours(self):
        hours = RECURSIVE_VALIDATION_HORIZON_STEPS * _RESAMPLE_MINUTES / 60
        self.assertAlmostEqual(hours, 4.0, places=6)

    def test_the_diagnostic_horizons_reach_day_ahead(self):
        """24 h is the horizon #937's -$0.71/day is actually scored at,
        so the curve is useless if it stops short of it."""
        longest = max(RECURSIVE_VALIDATION_DIAGNOSTIC_HORIZON_STEPS)
        hours = longest * _RESAMPLE_MINUTES / 60
        self.assertGreaterEqual(
            hours,
            24.0,
            "the diagnostic horizons do not reach day-ahead, which is "
            "the only horizon #937's dollar figure is measured at",
        )

    def test_the_diagnostic_horizons_are_all_longer_than_selection(self):
        """A horizon at or below the selection window adds cost and no
        information -- the selection metric already covers it."""
        for steps in RECURSIVE_VALIDATION_DIAGNOSTIC_HORIZON_STEPS:
            with self.subTest(steps=steps):
                self.assertGreater(steps, RECURSIVE_VALIDATION_HORIZON_STEPS)

    def test_the_extra_cost_is_bounded_and_stated(self):
        """This runs synchronously in an executor thread on every
        retrain, so an unbounded diagnostic is a real imposition on
        every install rather than a free measurement."""
        extra = RECURSIVE_VALIDATION_DIAGNOSTIC_MAX_ORIGINS * sum(
            RECURSIVE_VALIDATION_DIAGNOSTIC_HORIZON_STEPS
        )
        self.assertLessEqual(
            extra,
            2000,
            "the diagnostic walk got expensive -- origins x steps is the "
            f"cost, currently {extra} predict calls per retrain",
        )


class TestItCannotInfluenceSelection(unittest.TestCase):
    """The load-bearing property. A diagnostic that can change what gets
    deployed is not a diagnostic, and #937 is explicitly a measurement
    rather than a behaviour change."""

    def test_model_type_is_chosen_from_the_selection_metric_alone(self):
        chooser = re.search(r"model_type = min\((\w+), key=", _MODEL_SRC)
        self.assertIsNotNone(chooser, "the model_type selection line moved")
        self.assertEqual(
            chooser.group(1),
            "recursive_mae",
            "model_type is no longer chosen from the 16-step selection "
            "metric -- the #937 horizon curve is diagnostic and must "
            "never decide what gets deployed",
        )

    def test_nothing_reads_the_diagnostic_dict_for_a_decision(self):
        """Greps for the by-horizon dict being used in a comparison or
        min()/max(), which is what a decision would look like."""
        for pattern in (
            r"min\(\s*recursive_mae_by_horizon",
            r"max\(\s*recursive_mae_by_horizon",
            r"if\s+recursive_mae_by_horizon\[",
        ):
            with self.subTest(pattern=pattern):
                self.assertIsNone(
                    re.search(pattern, _MODEL_SRC),
                    "the diagnostic horizon dict is being used to decide something",
                )


class TestTheFieldSurvivesAnOldPickle(unittest.TestCase):
    """A new TrainedModel field is exactly the shape that has broken
    this project before -- `@dataclass` unpickling restores an old
    `__dict__` verbatim, skipping `__init__` and every
    `field(default_factory=...)`. `seasonal_lookup` raised
    `AttributeError` on the first `predict()` after deploy for precisely
    this reason (see model.py's own top-of-file account)."""

    def test_a_pickle_written_before_the_field_existed_still_reads(self):
        # Built the way unpickling builds it -- `object.__new__` plus a
        # restored `__dict__`, never `__init__` -- so no default_factory
        # ever runs. Constructing a real TrainedModel here would defeat
        # the point by filling the field in.
        model = TrainedModel.__new__(TrainedModel)
        model.__dict__.update({"model_type": "knn", "validation_mae": {}})
        self.assertNotIn("validation_recursive_mae_by_horizon", model.__dict__)
        self.assertEqual(
            getattr(model, "validation_recursive_mae_by_horizon", {}),
            {},
            "the defensive read used by coordinator.py must return an "
            "empty dict rather than raising on an older .pkl",
        )

    def test_the_unpickle_backfill_knows_about_it(self):
        """`__setstate__`'s own defaults dict is what makes an old pickle
        self-heal rather than relying on every reader being defensive."""
        self.assertIn(
            '"validation_recursive_mae_by_horizon": {},',
            _MODEL_SRC,
            "the new field is missing from the __setstate__ backfill, so "
            "an older .pkl restores without it",
        )


class TestItReachesAPublishedAttribute(unittest.TestCase):
    """The gap devhub caught on the first version of this change.

    The value was computed, stored on TrainedModel, and threaded
    into the coordinator's own training-info dict -- and stopped
    there. `sensor.py`'s `extra_state_attributes()` maps a fixed set
    of keys, and a key absent from that map is simply never
    published, so nothing outside the process could read it.

    That is the nimbus #1013 class exactly: wired into one layer,
    invisible at the next. A metric nobody can read back is not a
    metric, and this one exists purely to be read back.

    Source-text checks rather than a live HA harness, because the
    failure is a missing dict entry rather than a behaviour -- and
    the stub-based suite cannot build a real entity to ask.
    """

    def setUp(self):
        root = Path(__file__).resolve().parent.parent / "custom_components"
        self.sensor = (root / "nimbus_load" / "sensor.py").read_text(encoding="utf-8")
        self.coordinator = (root / "nimbus_load" / "coordinator.py").read_text(
            encoding="utf-8"
        )

    def test_the_coordinator_exports_both_metrics(self):
        for key in (
            "validation_recursive_mae",
            "validation_recursive_mae_by_horizon",
        ):
            with self.subTest(key=key):
                self.assertIn(
                    f'"{key}": getattr(',
                    self.coordinator,
                    f"{key} is not exported by the coordinator, or is "
                    "read non-defensively -- an older .pkl has neither",
                )

    def test_the_sensor_actually_publishes_them(self):
        """The half that was missing. Exporting from the coordinator
        is necessary and not sufficient."""
        for attr in (
            "ATTR_VALIDATION_RECURSIVE_MAE",
            "ATTR_VALIDATION_RECURSIVE_MAE_BY_HORIZON",
        ):
            with self.subTest(attr=attr):
                self.assertIn(
                    f"{attr}: data.get(",
                    self.sensor,
                    f"{attr} never reaches extra_state_attributes(), so "
                    "the value is computed and unreadable -- nimbus "
                    "#1013 class, caught by a devhub deploy check",
                )


if __name__ == "__main__":
    unittest.main()
