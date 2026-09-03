"""Real regression test for nimbus issue #347 (Mark Purcell): timezone was
hardcoded to Australia/Brisbane in solver_writer.py (17 sites).

Every hour-of-day decision (TOU fee lookup, the P2P window, midnight SoC
anchors, fixed-export blocks, quality-report day boundaries) ran in AEST
for every install, not just this household's own -- a Sydney/Melbourne
user during AEDT got all of these one hour late; a non-AU install was off
by many hours.

Fix: LOCAL_TZ resolves from the NIMBUS_SOLVER_TIMEZONE env var if set,
else this project's own existing Australia/Brisbane default (unchanged
behaviour with zero env vars set); set_native_hass() additionally
re-resolves it from hass.config.time_zone -- the household's own real,
already-configured timezone -- unless the env var was explicitly set,
which always wins.

Deliberately does NOT test the wall-clock-arithmetic-across-DST half of
issue #347 -- that's a separate, larger, deliberately-deferred fix to the
ML/solver time-grid construction itself, not something this narrower
timezone-RESOLUTION fix touches.

Imports and exercises the REAL set_native_hass() (not a reimplementation),
same "import solver_writer directly" pattern as its sibling solver_writer
test files.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import _solver_path  # noqa: F401
import solver_writer


class TestLocalTzResolution(unittest.TestCase):
    def setUp(self):
        self._orig_local_tz = solver_writer.LOCAL_TZ
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer.LOCAL_TZ = self._orig_local_tz
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def test_set_native_hass_resolves_local_tz_from_hass_config(self):
        solver_writer.LOCAL_TZ = ZoneInfo("Australia/Brisbane")  # the old default
        hass = MagicMock()
        hass.config.time_zone = "Australia/Sydney"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NIMBUS_SOLVER_TIMEZONE", None)
            solver_writer.set_native_hass(hass)

        self.assertEqual(solver_writer.LOCAL_TZ, ZoneInfo("Australia/Sydney"))

    def test_explicit_env_var_override_always_wins_over_hass_config(self):
        solver_writer.LOCAL_TZ = ZoneInfo(
            "Australia/Perth"
        )  # whatever the env var resolved at import
        hass = MagicMock()
        hass.config.time_zone = "Australia/Sydney"

        with patch.dict(
            os.environ, {"NIMBUS_SOLVER_TIMEZONE": "Australia/Perth"}, clear=False
        ):
            solver_writer.set_native_hass(hass)

        self.assertEqual(
            solver_writer.LOCAL_TZ,
            ZoneInfo("Australia/Perth"),
            "an explicit NIMBUS_SOLVER_TIMEZONE override must never be "
            "silently replaced by hass.config.time_zone",
        )

    def test_unresolvable_hass_config_time_zone_keeps_the_existing_value_and_never_raises(
        self,
    ):
        solver_writer.LOCAL_TZ = ZoneInfo("Australia/Brisbane")
        hass = MagicMock()
        hass.config.time_zone = "Not/A/Real/Zone"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NIMBUS_SOLVER_TIMEZONE", None)
            solver_writer.set_native_hass(hass)  # must not raise

        self.assertEqual(solver_writer.LOCAL_TZ, ZoneInfo("Australia/Brisbane"))

    def test_set_native_hass_still_sets_native_hass_regardless_of_tz_outcome(self):
        hass = MagicMock()
        hass.config.time_zone = "Not/A/Real/Zone"
        solver_writer.set_native_hass(hass)
        self.assertIs(solver_writer._NATIVE_HASS, hass)

    def test_hass_with_no_config_attribute_at_all_never_raises(self):
        """Real CI failure caught the first time this shipped: several
        existing tests call set_native_hass() with a deliberately narrow
        fake hass object that has NO .config attribute at all (unlike a
        MagicMock, which auto-creates one) -- a plain, non-defensive
        `hass.config.time_zone` raises AttributeError before the
        try/except around the ZoneInfo() call ever gets a chance to run.
        """
        solver_writer.LOCAL_TZ = ZoneInfo("Australia/Brisbane")

        class _BareHass:
            pass

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NIMBUS_SOLVER_TIMEZONE", None)
            solver_writer.set_native_hass(_BareHass())  # must not raise

        self.assertEqual(solver_writer.LOCAL_TZ, ZoneInfo("Australia/Brisbane"))

    def test_hass_none_never_raises(self):
        solver_writer.LOCAL_TZ = ZoneInfo("Australia/Brisbane")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NIMBUS_SOLVER_TIMEZONE", None)
            solver_writer.set_native_hass(None)  # must not raise

        self.assertEqual(solver_writer.LOCAL_TZ, ZoneInfo("Australia/Brisbane"))


if __name__ == "__main__":
    unittest.main()
