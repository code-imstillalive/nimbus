"""IV&V finding (4fd40c3..22205ad pass, 2026-09-26, head issue #1289, this
finding #1292): `7261fb0` ("#1256: carry the version stamp with the
computation, not with the process") built `_version_stamp()` specifically
because `_nimbus_version()` can return `None`, and an explicit
`"nimbus_version": None` is worse than an absent key -- the sensor layer's
own #972 fallback (`sensor.py`, `if "nimbus_version" in self._attrs: return
self._attrs`) declines to overwrite a key that is already present, so a
published `None` permanently suppresses the fallback that would otherwise
substitute the real running version.

`rescore_quality_history()` (`solver_writer.py:10008`) was not migrated:

    attrs["nimbus_version"] = _nimbus_version()

bypasses `_version_stamp()`'s None-guard entirely. This test reproduces the
exact bug #1256 fixed, through this one un-migrated call site: with
`_nimbus_version()` mocked to return None (a real, documented failure mode
-- missing/malformed manifest.json), rescoring a day must NOT publish an
explicit `"nimbus_version": None`.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import solver_writer


def _entry(status="applied"):
    return {
        "epr": 0.9,
        "j_ref": 5.0,
        "j_ach": -20.0,
        "j_star": -21.0,
        "regret_dollars": 1.0,
        "real_p2p_settlement_status": status,
    }


def _now(day=25):
    return datetime(2026, 9, day, 10, 0, tzinfo=solver_writer.LOCAL_TZ)


class TestRescoreNeverPublishesAnExplicitNoneVersion(unittest.TestCase):
    @pytest.mark.xfail(
        reason="nimbus #1292: rescore_quality_history() was not migrated to "
        "_version_stamp() and can publish an explicit nimbus_version: None, "
        "which the #972 sensor-layer fallback never corrects",
        strict=True,
    )
    def test_a_none_running_version_is_not_published_as_a_literal_none(self):
        posted: list[tuple[str, object, dict]] = []

        def _capture_post_state(entity_id, state, attrs):
            posted.append((entity_id, state, attrs))

        with (
            mock.patch.object(
                solver_writer,
                "_compute_report_for_window",
                side_effect=lambda cfg, day_start, day_end, allow_partial: _entry(
                    "applied"
                ),
            ),
            mock.patch.object(
                solver_writer,
                "ha_get",
                # latest_date must match the rescored day (2026-09-24, one
                # day back from _now(day=25)) so rescore_quality_history()'s
                # own `if key == latest_date:` headline-sync branch -- the
                # one containing the un-migrated nimbus_version assignment
                # this test targets -- actually executes.
                return_value={
                    "attributes": {"latest_date": "2026-09-24"},
                    "state": "90",
                },
            ),
            mock.patch.object(
                solver_writer, "ha_post_state", side_effect=_capture_post_state
            ),
            mock.patch.object(solver_writer, "_nimbus_version", return_value=None),
        ):
            solver_writer.rescore_quality_history({}, _now(day=25), 1)

        self.assertTrue(
            posted, "rescore_quality_history() must have published something"
        )
        _, _, attrs = posted[-1]
        self.assertNotIn(
            "nimbus_version",
            attrs,
            "an unknown running version must leave the key ABSENT (matching "
            "_version_stamp()'s own contract), not publish a literal None "
            f"that the #972 fallback can never correct -- got {attrs.get('nimbus_version')!r}",
        )


if __name__ == "__main__":
    unittest.main()
