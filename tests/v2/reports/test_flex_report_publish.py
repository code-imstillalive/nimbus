"""The daily flex report's publish step (#2393: solver_reports/flex.py's coverage, one behaviour per
test): yesterday already scored is re-pushed as it is (the freshness watchdog keeps seeing it), no report
leaves the sensor alone, and a new report is published with its total.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import solver_shared
import solver_writer as sw
from solver_reports import flex as flex_reports

NOW = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
YESTERDAY = "2026-11-08"


def _publish(existing, report):
    post = MagicMock()
    with (
        patch.object(sw, "ha_get", MagicMock(return_value=existing)),
        patch.object(sw, "ha_post_state", post),
        patch.object(solver_shared, "resolve_real_entity_id", lambda e: e),
        patch.object(
            flex_reports, "compute_daily_flex_report", MagicMock(return_value=report)
        ) as compute,
    ):
        flex_reports.publish_daily_flex_report({}, NOW)
    return post, compute


def test_a_day_already_scored_is_re_pushed_not_recomputed():
    # test_a_day_already_scored_is_re_pushed_not_recomputed
    existing = {
        "state": "7.5",
        "attributes": {"latest_date": YESTERDAY, "realised_up_kwh": 5.0},
    }
    post, compute = _publish(existing, None)
    compute.assert_not_called()
    post.assert_called_once_with(
        flex_reports.FLEX_REPORT_ENTITY_ID, "7.5", existing["attributes"]
    )


def test_no_report_yet_leaves_the_sensor_alone_and_a_new_one_is_published_with_its_total():
    # test_no_report_yet_leaves_the_sensor_alone_and_a_new_one_is_published_with_its_total
    older = {"state": "1", "attributes": {"latest_date": "2026-11-07"}}
    post, compute = _publish(older, None)
    compute.assert_called_once()
    post.assert_not_called()
    post, _compute = _publish(
        older,
        {"realised_up_kwh": 2.5, "realised_down_kwh": 1.25, "latest_date": YESTERDAY},
    )
    entity, state, attrs = post.call_args.args
    assert entity == flex_reports.FLEX_REPORT_ENTITY_ID
    assert state == 3.75
    assert attrs["unit_of_measurement"] == "kWh" and attrs["latest_date"] == YESTERDAY
