"""Regression test for nimbus issue #375 (Mark Purcell's own agent, filed as
a sub-issue of #371's codebase review): LTS/hybrid rows never become
training rows.

coordinator.py's `lts`/`hybrid` training sources feed train_model() events
derived from HA's hourly long-term-statistics buckets -- a coarser cadence
than this module's own 15-min training grid. resample_observed_mask()
(#350) correctly marks only the FIRST grid point after each new hourly
event as a genuine observation; everything else that hour is a pure
forward-fill carry-over. But that one genuine observation's own lag_short
lookup (train_model()'s "value one grid step, i.e. RESAMPLE_MINUTES=15,
ago") lands on the PREVIOUS hour's forward-filled tail -- for a grid whose
own offset doesn't line up exactly on the hour (which real wall-clock grids
essentially never do), that tail point can be up to just under an hour
stale relative to the PREVIOUS hour's own event. The default
max_staleness cap (RESAMPLE_MINUTES * MAX_TRAINING_STALENESS_GRID_STEPS =
15 * 3 = 45 minutes, #353's own outage-detection guard, sized for
recorder-cadence data) rejects that lookup as stale and returns None for
it -- resample_last_value() then makes the WHOLE row's lag_short_v None,
and train_model()'s own row loop drops any row with a None lag input,
before resample_observed_mask()'s own skip-if-not-observed check is even
reached. The result: every single LTS-derived training row is silently
dropped, not just the forward-fill duplicates #350 meant to remove.

Confirmed live: a real install training on 60 days of hybrid history
(55 days LTS + 5 days recorder) produced only ~470 usable rows -- exactly
"5 days at native cadence," meaning the entire 55-day LTS contribution
was silently zero. Also reproduced independently on devhub the same way
after updating to the pre-fix release (v0.94.116): "Only 46/32/31/56/276
usable training points" log lines across several subentries -- the same
symptom, not install-specific.

Fixed with a new `max_staleness_minutes` parameter on train_model():
coordinator.py's own `_LTS_PERIOD_MINUTES + RESAMPLE_MINUTES` (60 + 15 =
75 minutes) is wide enough to cover the worst-case ~59-minute gap between
an hourly LTS event and the previous hour's own forward-filled tail, while
still catching a genuine multi-hour+ outage exactly like #353 always did.

Real, from-scratch synthetic data through the actual train_model() --
not a reimplementation -- same "ml/model.py has zero homeassistant.*
imports, directly importable" convention as this project's other ml test
files, including test_train_model_skips_forward_filled_duplicate_rows.py
(#350's own test, which this issue's fix must not regress -- see the
mutation-testing note in this file's own commit).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import _ml_path  # noqa: F401
from nimbus_load.ml import model as ml_model

START_OF_TIME = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001


class TestTrainModelLtsStalenessCap(unittest.TestCase):
    def _hourly_events_on_offset_grid(self, days: int):
        """30 days of real hourly-cadence events (the issue's own reported
        shape: HA long-term-statistics buckets), plus a couple of extra
        hours past `end` so every grid point inside the window has a real
        event to resample from. `start` is offset 5 minutes past the hour
        -- a deliberately NON-zero offset, the same as any real wall-clock
        grid genuinely has relative to the top of an hour -- so no grid
        point ever lands exactly on an hourly event's own timestamp.
        """
        n_hours = days * 24 + 2
        events = [
            (START_OF_TIME + timedelta(hours=h), 3.0 + 0.013 * h)
            for h in range(n_hours)
        ]
        start = START_OF_TIME + timedelta(minutes=5)
        end = START_OF_TIME + timedelta(days=days)
        return events, start, end

    def test_default_staleness_cap_drops_every_lts_row(self):
        """Reproduces the live bug exactly: with the old, unwidened default
        cap (max_staleness_minutes=None -> 45 minutes), every observed
        hourly row's own lag_short lookup lands ~50 minutes stale and gets
        rejected -- zero usable rows survive, so train_model() returns
        None outright."""
        days = 30
        events, start, end = self._hourly_events_on_offset_grid(days)

        trained = ml_model.train_model(
            load_events=events,
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=start,
            end=end,
            resample_minutes=15,
            min_training_points=10,
        )

        self.assertIsNone(
            trained,
            "train_model() produced a model from the unfixed 45-min cap -- "
            "the live bug (zero usable LTS rows) did not reproduce",
        )

    def test_widened_staleness_cap_recovers_the_lts_rows(self):
        """The fix: coordinator.py's own 75-minute cap
        (_LTS_PERIOD_MINUTES + RESAMPLE_MINUTES) is wide enough to accept
        the same lag lookup the default cap rejects -- almost every real
        hourly observation (~24/day) now survives as a training row,
        recovering the entire LTS contribution the live bug silently
        dropped."""
        days = 30
        events, start, end = self._hourly_events_on_offset_grid(days)

        trained = ml_model.train_model(
            load_events=events,
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=start,
            end=end,
            resample_minutes=15,
            min_training_points=10,
            max_staleness_minutes=75,
        )

        self.assertIsNotNone(trained)
        # ~24 genuine hourly observations/day over 30 days, minus a
        # handful trimmed at the LAG_LONG_STEPS-into-the-grid start edge --
        # a generous band, not an exact count, since the exact edge loss
        # depends on where the offset grid's first few points fall.
        self.assertGreater(trained.training_points, days * 20)
        self.assertLessEqual(trained.training_points, days * 24)
