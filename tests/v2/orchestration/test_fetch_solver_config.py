"""Fetching the solver config gives a clear, actionable error, never a raw HTTP
one (#831) (#1837).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import solver_writer

fetch_solver_config = solver_writer.fetch_solver_config


def test_entity_does_not_exist_yet_raises_a_clear_runtime_error():
    # test_entity_does_not_exist_yet_raises_a_clear_runtime_error
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

    # test_a_non_404_http_error_still_propagates_unmasked
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
