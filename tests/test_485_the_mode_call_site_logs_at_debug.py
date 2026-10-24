"""nimbus #485: the household-mode call site logs which load moved, at
DEBUG. A check that reads source text, kept apart until the decision on
source-reading tests (#1837); the presets themselves are in
tests/v2/entities/test_household_modes.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestThePerLoadPresetIsObservable(unittest.TestCase):
    def test_the_call_site_logs_at_debug_and_names_the_load(self):
        """Source-level, because the alternative is driving a full native
        solve to observe a log line. Checks the three things that make
        the line useful: the level, the mode, and WHICH load — a six-load
        install needs to know which one moved."""
        # nimbus #1302 (spec 003): the call site moved to
        # solver_inputs/controllable_loads.py with build_controllable_loads.
        # writer_source() is the union across solver_writer.py and every
        # module #1298 has extracted out of it, which is the property this
        # test was always asserting -- not which file the literal lives in.
        from _writer_source import writer_source

        src = writer_source()
        i = src.index("household_modes.apply_to_load_config")
        # Wide enough to reach the whole log call. An earlier 900
        # truncated mid-argument and failed on `title` -- the window
        # was wrong, not the assertion.
        window = src[i : i + 1400]
        self.assertIn("_LOGGER.debug(", window)
        self.assertIn("#485", window)
        self.assertIn("title", window)
        self.assertNotIn("_LOGGER.info(", window)
