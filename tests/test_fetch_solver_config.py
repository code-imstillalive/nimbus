"""nimbus issue #831: real tests for solver_writer.fetch_solver_config()'s
own error handling -- found live on devhub, a raw urllib.error.HTTPError
(404, entity doesn't exist yet) leaking straight through a function whose
own docstring says it exists specifically to give a clear, actionable
error instead of a confusing low-level one.

Same convention as test_resolve_envelope_limit_kw.py -- imports the real
function directly, monkeypatches solver_writer.ha_get (not urllib itself)
to fake a single HA API call without a full HA stub environment.
"""

from __future__ import annotations

from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_writer

fetch_solver_config = solver_writer.fetch_solver_config


def test_entity_does_not_exist_yet_raises_a_clear_runtime_error():
    def _raise(entity_id):
        raise solver_writer.urllib.error.HTTPError(
            url=None,
            code=404,
            msg="Entity sensor.nimbus_solver_config not found",
            hdrs=None,
            fp=None,
        )

    with (
        patch.object(solver_writer, "ha_get", _raise),
        pytest.raises(RuntimeError) as exc_info,
    ):
        fetch_solver_config()
    # Not the raw HTTPError text -- a real, actionable message.
    assert "HTTP Error 404" not in str(exc_info.value)
    assert "does not exist yet" in str(exc_info.value)


def test_a_non_404_http_error_still_propagates_unmasked():
    # Only the specific "entity doesn't exist" case gets a friendlier
    # message -- a genuine server-side failure (500, etc.) must not be
    # silently reframed as "doesn't exist yet", which would be actively
    # misleading.
    def _raise(entity_id):
        raise solver_writer.urllib.error.HTTPError(
            url=None, code=500, msg="Internal Server Error", hdrs=None, fp=None
        )

    with (
        patch.object(solver_writer, "ha_get", _raise),
        pytest.raises(solver_writer.urllib.error.HTTPError) as exc_info,
    ):
        fetch_solver_config()
    assert exc_info.value.code == 500


def test_entity_exists_but_not_configured_still_raises_its_own_existing_message():
    # The pre-existing "configured" check is unaffected by this fix.
    def _mock_ha_get(entity_id):
        return {"entity_id": entity_id, "state": "not_configured", "attributes": {}}

    with (
        patch.object(solver_writer, "ha_get", _mock_ha_get),
        pytest.raises(RuntimeError) as exc_info,
    ):
        fetch_solver_config()
    assert "not configured yet" in str(exc_info.value)


def test_entity_exists_and_configured_returns_its_attributes():
    def _mock_ha_get(entity_id):
        return {
            "entity_id": entity_id,
            "state": "configured",
            "attributes": {"solver_battery_capacity_kwh": 13.5},
        }

    with patch.object(solver_writer, "ha_get", _mock_ha_get):
        result = fetch_solver_config()
    assert result == {"solver_battery_capacity_kwh": 13.5}
