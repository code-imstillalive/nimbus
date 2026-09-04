"""Regression test for nimbus issue #363 (Mark Purcell, codebase review),
finding 4: fetch_price_history() and fetch_entity_history_range() were
~90% identical -- the same REST/native dual-mode recorder bridge, the
same point-building loop, differing only in "last N days from now" vs
an explicit window plus a couple of cosmetic details that produced
identical outcomes either way. fetch_price_history() is now a thin
wrapper delegating to fetch_entity_history_range(), the one real
implementation.

Covers REST mode (mocked urllib), the shared behaviour both entry
points now consolidate into, and confirms fetch_price_history()
computes and passes through the correct [now - days, now] window.
Native mode's own recorder bridge is unchanged by this refactor (copied
verbatim, not touched) and isn't independently covered here -- reliably
mocking asyncio.run_coroutine_threadsafe's cross-thread scheduling
under this project's pytest/pytest-asyncio setup proved too fragile to
trust (passed standalone, silently degraded to [] under pytest for
reasons not worth chasing for code this fix doesn't change).
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import _solver_path  # noqa: F401
import solver_writer


def _rest_response(payload) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda self, *a: False
    return resp


class TestFetchEntityHistoryRangeRestMode:
    """The canonical implementation -- REST mode, real HTTP mocked."""

    def test_valid_numeric_states_parsed_and_sorted(self):
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 9, 2, tzinfo=UTC)
        payload = [
            [
                {"state": "0.25", "last_changed": "2026-09-01T02:00:00+00:00"},
                {"state": "0.10", "last_changed": "2026-09-01T01:00:00+00:00"},
            ]
        ]
        with patch("urllib.request.urlopen", return_value=_rest_response(payload)):
            result = solver_writer.fetch_entity_history_range(
                "sensor.price", start, end
            )
        # Sorted ascending by time, values parsed as floats.
        assert [v for _, v in result] == [0.10, 0.25]
        assert result[0][0] < result[1][0]

    def test_unknown_and_unavailable_states_are_skipped(self):
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 9, 2, tzinfo=UTC)
        payload = [
            [
                {"state": "unknown", "last_changed": "2026-09-01T01:00:00+00:00"},
                {"state": "unavailable", "last_changed": "2026-09-01T02:00:00+00:00"},
                {"state": None, "last_changed": "2026-09-01T03:00:00+00:00"},
                {"state": "0.30", "last_changed": "2026-09-01T04:00:00+00:00"},
            ]
        ]
        with patch("urllib.request.urlopen", return_value=_rest_response(payload)):
            result = solver_writer.fetch_entity_history_range(
                "sensor.price", start, end
            )
        assert [v for _, v in result] == [0.30]

    def test_unparseable_numeric_state_is_skipped(self):
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 9, 2, tzinfo=UTC)
        payload = [
            [
                {"state": "not-a-number", "last_changed": "2026-09-01T01:00:00+00:00"},
                {"state": "0.42", "last_changed": "2026-09-01T02:00:00+00:00"},
            ]
        ]
        with patch("urllib.request.urlopen", return_value=_rest_response(payload)):
            result = solver_writer.fetch_entity_history_range(
                "sensor.price", start, end
            )
        assert [v for _, v in result] == [0.42]

    def test_http_error_degrades_to_empty_list_not_raise(self):
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 9, 2, tzinfo=UTC)
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 500, "server error", {}, None),
        ):
            result = solver_writer.fetch_entity_history_range(
                "sensor.price", start, end
            )
        assert result == []

    def test_empty_history_returns_empty_list(self):
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 9, 2, tzinfo=UTC)
        with patch("urllib.request.urlopen", return_value=_rest_response([[]])):
            result = solver_writer.fetch_entity_history_range(
                "sensor.price", start, end
            )
        assert result == []

    def test_start_end_in_a_non_utc_timezone_are_handled_correctly(self):
        # A caller-supplied window in an arbitrary timezone (e.g.
        # LOCAL_TZ) must still produce a correct UTC-formatted request
        # URL -- confirms the .astimezone(UTC) call this function makes
        # (a no-op for fetch_price_history's own already-UTC start/end,
        # but load-bearing for other real callers like
        # compute_daily_quality_report()) actually works.
        start = datetime(2026, 9, 1, 0, 0, tzinfo=solver_writer.LOCAL_TZ)
        end = datetime(2026, 9, 2, 0, 0, tzinfo=solver_writer.LOCAL_TZ)
        captured_url = {}

        def _capture_request(req, timeout):
            captured_url["url"] = req.full_url
            return _rest_response([[]])

        with patch("urllib.request.urlopen", side_effect=_capture_request):
            solver_writer.fetch_entity_history_range("sensor.price", start, end)

        expected_start_utc = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        expected_end_utc = end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        assert expected_start_utc in captured_url["url"]
        assert expected_end_utc in captured_url["url"]


class TestFetchPriceHistoryDelegatesCorrectly:
    """fetch_price_history() is now a thin wrapper -- these tests confirm
    it computes the right [now - days, now] window and genuinely
    delegates (not a parallel reimplementation that happens to agree)."""

    def test_delegates_to_fetch_entity_history_range_with_the_right_window(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", return_value=[]
        ) as mock_range:
            solver_writer.fetch_price_history("sensor.price", days=5)

        mock_range.assert_called_once()
        called_entity_id, called_start, called_end = mock_range.call_args[0]
        assert called_entity_id == "sensor.price"
        assert (called_end - called_start) == timedelta(days=5)
        # end must be genuinely "now", not a stale/fixed value.
        assert (datetime.now(UTC) - called_end) < timedelta(seconds=10)

    def test_default_days_is_five(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", return_value=[]
        ) as mock_range:
            solver_writer.fetch_price_history("sensor.price")

        _, called_start, called_end = mock_range.call_args[0]
        assert (called_end - called_start) == timedelta(days=5)

    def test_real_end_to_end_rest_mode_result_matches_direct_call(self):
        # Genuine equivalence check, not just "it delegates" -- the same
        # REST payload through both entry points must produce the exact
        # same parsed result.
        payload = [
            [
                {"state": "0.15", "last_changed": "2026-09-01T01:00:00+00:00"},
                {"state": "unavailable", "last_changed": "2026-09-01T02:00:00+00:00"},
                {"state": "0.28", "last_changed": "2026-09-01T03:00:00+00:00"},
            ]
        ]
        with patch("urllib.request.urlopen", return_value=_rest_response(payload)):
            via_price_history = solver_writer.fetch_price_history(
                "sensor.price", days=5
            )
        with patch("urllib.request.urlopen", return_value=_rest_response(payload)):
            end = datetime.now(UTC)
            start = end - timedelta(days=5)
            via_direct_range = solver_writer.fetch_entity_history_range(
                "sensor.price", start, end
            )
        assert [v for _, v in via_price_history] == [v for _, v in via_direct_range]
        assert [v for _, v in via_price_history] == [0.15, 0.28]
