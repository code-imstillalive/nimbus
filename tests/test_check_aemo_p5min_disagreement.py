"""Real tests for solver_writer.check_aemo_p5min_disagreement() -- nimbus
issue #452 (Mark Purcell): cross-check the current interval's real
retail commodity price against AEMO's own live P5MIN wholesale price,
flagging a disagreement beyond the household's own typical same-time-
of-day retail markup.

Same convention as test_resolve_envelope_limit_kw.py -- imports the
real function directly, monkeypatches solver_writer.ha_get (not
urllib itself) to fake a single HA API call without a full HA stub
environment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

check_aemo_p5min_disagreement = solver_writer.check_aemo_p5min_disagreement
_warn_aemo_p5min_disagreement_once = solver_writer._warn_aemo_p5min_disagreement_once

_TZ = timezone(timedelta(hours=10))  # Australia/Brisbane, no DST


def _mock_ha_get(state):
    def _fn(entity_id):
        return {"entity_id": entity_id, "state": state, "attributes": {}}

    return _fn


def test_no_sensor_configured_returns_none():
    result = check_aemo_p5min_disagreement(None, 0.30, 10, {10: 0.20}, 0.10)
    assert result is None


def test_no_offset_history_for_this_bucket_returns_none():
    with patch.object(solver_writer, "ha_get", _mock_ha_get("0.10")):
        result = check_aemo_p5min_disagreement(
            "sensor.aemo_nem_qld1_current_5min_period_price", 0.30, 10, {}, 0.10
        )
    assert result is None


def test_unavailable_state_returns_none_not_a_crash():
    with patch.object(solver_writer, "ha_get", _mock_ha_get("unavailable")):
        result = check_aemo_p5min_disagreement(
            "sensor.aemo_nem_qld1_current_5min_period_price", 0.30, 10, {10: 0.20}, 0.10
        )
    assert result is None


def test_read_failure_returns_none_not_a_crash():
    def _raise(entity_id):
        raise solver_writer.urllib.error.URLError("boom")

    with patch.object(solver_writer, "ha_get", _raise):
        result = check_aemo_p5min_disagreement(
            "sensor.aemo_nem_qld1_current_5min_period_price", 0.30, 10, {10: 0.20}, 0.10
        )
    assert result is None


def test_agreement_within_normal_markup_is_not_flagged():
    # AEMO now = $0.10/kWh, this bucket's typical markup = $0.20/kWh ->
    # expected retail = $0.30/kWh, and that's exactly what retail reads.
    with patch.object(solver_writer, "ha_get", _mock_ha_get("0.10")):
        result = check_aemo_p5min_disagreement(
            "sensor.aemo_nem_qld1_current_5min_period_price", 0.30, 10, {10: 0.20}, 0.10
        )
    assert result["flagged"] is False
    assert result["disagreement_dollars"] == 0.0
    assert result["expected_retail"] == 0.3


def test_disagreement_beyond_threshold_is_flagged():
    # Expected retail = 0.10 + 0.20 = $0.30/kWh, real retail is $0.55/kWh
    # -- a $0.25 residual, beyond the $0.10 threshold.
    with patch.object(solver_writer, "ha_get", _mock_ha_get("0.10")):
        result = check_aemo_p5min_disagreement(
            "sensor.aemo_nem_qld1_current_5min_period_price", 0.55, 10, {10: 0.20}, 0.10
        )
    assert result["flagged"] is True
    assert abs(result["disagreement_dollars"] - 0.25) < 1e-9


def test_disagreement_exactly_at_threshold_is_not_flagged():
    # Strictly-greater-than semantics, matching every other threshold
    # comparison this project uses (e.g. soc_discrepancy_reliable).
    with patch.object(solver_writer, "ha_get", _mock_ha_get("0.10")):
        result = check_aemo_p5min_disagreement(
            "sensor.aemo_nem_qld1_current_5min_period_price", 0.40, 10, {10: 0.20}, 0.10
        )
    assert result["flagged"] is False


def test_negative_disagreement_beyond_threshold_is_also_flagged():
    # Real retail LOWER than expected is just as much a genuine
    # disagreement as higher -- abs(), not a one-sided check.
    with patch.object(solver_writer, "ha_get", _mock_ha_get("0.10")):
        result = check_aemo_p5min_disagreement(
            "sensor.aemo_nem_qld1_current_5min_period_price", 0.05, 10, {10: 0.20}, 0.10
        )
    assert result["flagged"] is True
    assert result["disagreement_dollars"] < 0


def test_warn_once_logs_only_the_first_time_for_a_given_period(caplog):
    solver_writer._AEMO_P5MIN_LAST_WARNED_PERIOD = None
    period = datetime(2026, 9, 13, 12, 0, tzinfo=_TZ)
    detail = {
        "retail_now": 0.55,
        "aemo_p5min_now": 0.10,
        "expected_retail": 0.30,
        "disagreement_dollars": 0.25,
        "threshold_dollars": 0.10,
    }
    with caplog.at_level("WARNING"):
        _warn_aemo_p5min_disagreement_once(period, detail)
        _warn_aemo_p5min_disagreement_once(period, detail)
    matching = [r for r in caplog.records if "Nimbus #452" in r.message]
    assert len(matching) == 1


def test_warn_once_logs_again_for_a_genuinely_new_period(caplog):
    solver_writer._AEMO_P5MIN_LAST_WARNED_PERIOD = None
    period_a = datetime(2026, 9, 13, 12, 0, tzinfo=_TZ)
    period_b = datetime(2026, 9, 13, 12, 5, tzinfo=_TZ)
    detail = {
        "retail_now": 0.55,
        "aemo_p5min_now": 0.10,
        "expected_retail": 0.30,
        "disagreement_dollars": 0.25,
        "threshold_dollars": 0.10,
    }
    with caplog.at_level("WARNING"):
        _warn_aemo_p5min_disagreement_once(period_a, detail)
        _warn_aemo_p5min_disagreement_once(period_b, detail)
    matching = [r for r in caplog.records if "Nimbus #452" in r.message]
    assert len(matching) == 2
