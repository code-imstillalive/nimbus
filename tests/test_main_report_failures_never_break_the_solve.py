"""nimbus issue #735: the six report publishes in `main()` are wrapped so
that a failure in any of them "must never take down the real solve" --
and until now that invariant lived only in the comments beside them.

This is the same gap stage 5 of that issue found in the SoC-envelope
block, and it is worth closing for the same reason: #735's own plan moves
these calls into `solver_reports/` on their own timer. Refactoring a
guard that nothing tests is how a guard quietly stops guarding.

The history makes the case better than the plan does. Every one of these
`except` branches used to be a bare `pass`, and the comments record what
that cost:

    2026-08-31: previously a bare `pass` -- made the entity-id-collision
    incident this file's own resolve_real_entity_id() fixes completely
    invisible in the log for days (confirmed live on devhub: 200+ recent
    log lines matching "nimbus", zero exceptions, zero tracebacks,
    because every failure here was silently swallowed).

So there are two invariants here, not one, and the second is the one that
actually broke in production:

1. A failing report must not stop the solve or the dispatch push.
2. A failing report must be **logged**, not silently swallowed.

A regression to a bare `pass` satisfies (1) perfectly and re-breaks (2)
invisibly. Both are asserted below, per publisher.

Driven through the real `main()` using the same fixture as
`test_main_golden_output_guardrail.py`, so what is tested is the actual
call site rather than a reconstruction of it.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_writer
from test_main_golden_output_guardrail import (
    _HEALTHY_LOAD_STATE,
    _LOAD_SENSOR,
    _SOLVER_CONFIG_ATTRS,
)

# name in solver_writer -> the distinctive phrase its guard must log
_REPORT_PUBLISHERS = {
    "publish_weather_forecast_mirrors": "weather forecast mirror publish failed",
    "publish_daily_quality_report": "daily quality report publish failed",
    "publish_daily_flex_report": "daily flex report publish failed",
    "publish_nimbus_only_soc_counterfactual": "counterfactual SoC publish failed",
    "publish_efficiency_backtest_report": "efficiency backtest publish failed",
    "update_solar_delivery_ratio": "solar delivery ratio update failed",
}


def _make_ha_get():
    known = {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": _SOLVER_CONFIG_ATTRS,
        },
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
        "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        _LOAD_SENSOR: _HEALTHY_LOAD_STATE,
    }

    def _ha_get(entity_id: str):
        if entity_id in known:
            return known[entity_id]
        raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)

    return _ha_get


class _PatchAll:
    """Patch several solver_writer attributes to the same side effect."""

    def __init__(self, names, side_effect):
        self._names = names
        self._side_effect = side_effect
        self._patches: list = []

    def __enter__(self):
        for name in self._names:
            p = patch.object(solver_writer, name, side_effect=self._side_effect)
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *_exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _run_main(failing):
    """Run the real main() with `failing` publishers raising. Returns the
    dict of entity_id -> (state, attrs) that actually got pushed."""
    posted = {}

    def _capture(entity_id, state, attrs=None):
        posted[entity_id] = (state, attrs)

    def _boom(*_a, **_kw):
        raise RuntimeError("synthetic report failure")

    with (
        patch.object(solver_writer, "ha_get", side_effect=_make_ha_get()),
        patch.object(solver_writer, "ha_post_state", side_effect=_capture),
        patch.object(solver_writer, "acquire_lock", return_value=True),
        patch.object(solver_writer, "release_lock"),
        patch.object(
            solver_writer,
            "PLAN_STATE_PATH",
            "/tmp/nonexistent_plan_state_report_isolation_test.json",
        ),
        _PatchAll(failing, _boom),
    ):
        solver_writer.main()
    return posted


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
class TestOneFailingReportNeverBreaksTheSolve:
    @pytest.mark.parametrize("publisher", sorted(_REPORT_PUBLISHERS))
    def test_the_plan_is_still_published(self, freezer, publisher):
        """Invariant 1: dispatch survives. The plan sensor is what the
        household's battery actually follows."""
        posted = _run_main([publisher])
        assert solver_writer.ENTITY_ID in posted, (
            f"{publisher} raising stopped main() from publishing the plan at "
            "all -- the 'must never take down the real solve' guard around "
            "it is not holding"
        )
        _state, attrs = posted[solver_writer.ENTITY_ID]
        assert attrs["status"] == "optimal"

    @pytest.mark.parametrize("publisher", sorted(_REPORT_PUBLISHERS))
    def test_the_failure_is_logged_not_swallowed(self, freezer, caplog, publisher):
        """Invariant 2, and the one that actually broke in production: a
        regression to a bare `except: pass` would keep every assertion
        above green and re-create the incident where failures were
        invisible in the log for days."""
        with caplog.at_level("WARNING"):
            _run_main([publisher])
        assert _REPORT_PUBLISHERS[publisher] in caplog.text, (
            f"{publisher} failed silently -- nothing in the log names it. "
            "That is exactly the bare-`pass` behaviour the 2026-08-31 "
            "comments were written about."
        )


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
class TestEveryReportFailingAtOnce:
    """The realistic shape of a bad deploy or an HA-side outage: these
    share helpers, and a broken one is rarely broken alone."""

    def test_the_solve_still_completes_and_publishes(self, freezer):
        posted = _run_main(sorted(_REPORT_PUBLISHERS))
        assert solver_writer.ENTITY_ID in posted
        _state, attrs = posted[solver_writer.ENTITY_ID]
        assert attrs["status"] == "optimal"
        assert attrs["forecast"], "the plan must still carry a real forecast"

    def test_every_one_of_them_is_named_in_the_log(self, freezer, caplog):
        with caplog.at_level("WARNING"):
            _run_main(sorted(_REPORT_PUBLISHERS))
        missing = [
            name
            for name, phrase in _REPORT_PUBLISHERS.items()
            if phrase not in caplog.text
        ]
        assert not missing, f"failed silently with everything broken: {missing}"


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
class TestTheBaselineIsClean:
    """Guards every assertion above from passing vacuously: with nothing
    patched to fail, none of those phrases should appear at all."""

    def test_no_failure_warnings_when_nothing_is_broken(self, freezer, caplog):
        with caplog.at_level("WARNING"):
            posted = _run_main([])
        assert solver_writer.ENTITY_ID in posted
        spurious = [
            name for name, phrase in _REPORT_PUBLISHERS.items() if phrase in caplog.text
        ]
        assert not spurious, (
            f"these reported a failure on a healthy run: {spurious} -- either "
            "a real bug, or this fixture no longer reaches them"
        )
