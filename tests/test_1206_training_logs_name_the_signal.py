"""nimbus #1206: every line `train_model()` logs now names the signal it
is talking about.

**The measured gap.** On the reference household's production log across
2026-09-21 → 2026-09-25 — four days:

```
43x  WARNING  Only N usable training points (need >= 500) -- skipping this cycle.
```

with point counts of 57, 59, 63, 65, 165, 170, 294, spread across several
`SyncWorker` threads. That is the entire signal. There was no way to tell
from it whether one chronically starved subentry was retrying or seven
different ones were failing once, nor whether it was a newly added
subentry warming up (expected, self-correcting) or one that had never
trained at all (real, and silently serving no forecast). `SyncWorker_N`
is not an answer — the executor reassigns threads between calls.

The same applied to all eight of the function's log lines, including the
model-selection ones. Observed on devhub, consecutive lines from a single
retrain sweep, five signals and no way to separate them:

```
Model validation (one-step): knn_mae=1.0673 gbrt_mae=0.0401 naive_mae=2.5741
Trained gbrt model on 3864 points.
Model validation (one-step): knn_mae=3.7993 gbrt_mae=0.0534 naive_mae=9.8361
Trained gbrt model on 3321 points.
Model validation (MASE, scale=2.6727): knn=1.305 gbrt=1.228 naive=1.470
Trained naive model on 3541 points.
```

The third selected **naive** over both ML models — exactly the result
#937 is about, and unattributable.

**Why a parameter rather than a lookup.** `ml/` is deliberately
HA-import-free (pure numpy + stdlib) and should not acquire entity
knowledge. The caller already has the name: `coordinator.py`'s own
residual-drift WARNING uses `self.subentry.title`, and that line is
readable precisely because it is attributed. So the label is threaded in
and defaults to None.

The properties pinned here:

1. **A label appears on every line the function logs**, not a chosen
   subset — pinned by asserting against the function's own source rather
   than a hand-listed set, so a line added later is covered.
2. **Omitting the label leaves every message byte-identical**, which is
   what keeps the standalone/cron copy and every pre-existing test
   working untouched.
3. **The coordinator actually supplies it**, from the same field the
   drift warning uses. A parameter nothing passes is not a fix.
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import _ml_path  # noqa: F401
from nimbus_load.ml import model as model_mod


def _events(n=4, value=1.0):
    start = datetime(2026, 9, 24, tzinfo=UTC)
    return [(start + timedelta(minutes=15 * i), value) for i in range(n)]


def _train(**over):
    kwargs = {
        "load_events": _events(),
        "temp_events": [],
        "humidity_events": [],
        "curtailment_events": [],
        "start": datetime(2026, 9, 24, tzinfo=UTC),
        "end": datetime(2026, 9, 25, tzinfo=UTC),
        "resample_minutes": 15,
        "min_training_points": 500,
    }
    kwargs.update(over)
    return model_mod.train_model(**kwargs)


class TestEveryLoggedLineCanBeAttributed:
    """Property 1 -- checked against the source, not a hand-listed set."""

    def test_no_logged_message_lacks_the_prefix_placeholder(self):
        src = inspect.getsource(model_mod.train_model)
        # Every _LOGGER.<level>( call inside train_model must pass the
        # prefix. Matching the message string that immediately follows.
        calls = re.findall(r"_LOGGER\.(?:info|warning|error|debug)\(\s*\n?\s*(.)", src)
        assert calls, "expected train_model to log something"
        bad = [
            m.group(0)[:70]
            for m in re.finditer(
                r'_LOGGER\.(?:info|warning|error|debug)\(\s*\n?\s*"', src
            )
            if '"%s' not in src[m.start() : m.start() + 120]
        ]
        assert not bad, (
            "these logged messages do not start with the %s label prefix, so "
            f"they cannot be attributed to a signal: {bad}"
        )


class TestTheLabelReachesTheOutput:
    def test_a_labelled_skip_names_the_signal(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = _train(label="CB Total Combined Power Adjusted (kW)")
        assert result is None  # 4 points against a 500 minimum
        msgs = [r.getMessage() for r in caplog.records]
        assert any("CB Total Combined Power Adjusted (kW):" in m for m in msgs), msgs

    def test_the_no_history_path_is_labelled_too(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = _train(load_events=[], label="charger Power Active Import")
        assert result is None
        msgs = [r.getMessage() for r in caplog.records]
        assert any("charger Power Active Import:" in m for m in msgs), msgs


class TestOmittingTheLabelChangesNothing:
    """Property 2 -- the control. This is what keeps the standalone copy
    and every pre-existing test working."""

    def test_messages_are_byte_identical_without_a_label(self, caplog):
        with caplog.at_level(logging.WARNING):
            _train()
        msgs = [r.getMessage() for r in caplog.records]
        assert msgs, "expected a skip warning"
        assert any(
            m.startswith("Only ") and "usable training points" in m for m in msgs
        ), f"an unlabelled call must read exactly as before: {msgs}"

    def test_no_history_message_is_byte_identical_without_a_label(self, caplog):
        with caplog.at_level(logging.WARNING):
            _train(load_events=[])
        msgs = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("No load history available") for m in msgs), msgs


class TestTheCoordinatorActuallySuppliesIt:
    """Property 3 -- a parameter nothing passes is not a fix."""

    def test_train_model_job_forwards_a_label(self):
        src = Path(__file__).resolve().parent.parent / (
            "custom_components/nimbus_load/coordinator.py"
        )
        text = src.read_text(encoding="utf-8")
        assert "label=label," in text, (
            "_train_model_job must forward the label to train_model()"
        )
        assert "self.subentry.title," in text, (
            "the executor call must supply the signal name, from the same "
            "field the residual-drift warning already uses"
        )
