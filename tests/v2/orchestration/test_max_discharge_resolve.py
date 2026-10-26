"""The maximum discharge comes from the configured value or a configured live
entity, never an entity found by name (#125) (#1837).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import solver_writer


def _cfg(**overrides):
    base = {"solver_max_discharge_kw": 24.0}
    base.update(overrides)
    return base


class TestMalformedLiveEntityDegradesGracefully(unittest.TestCase):
    """A configured live entity that exists but doesn't have a usable
    numeric 'max' attribute must never crash the solve -- same
    honest-fallback-over-crash discipline as every other real external
    read in this file."""

    def test_missing_attributes_key_entirely_falls_back(self):
        with (
            patch.object(solver_writer, "entity_exists", return_value=True),
            patch.object(solver_writer, "ha_get", return_value={}),
        ):
            result = solver_writer.resolve_max_discharge_kw(
                _cfg(solver_max_discharge_live_entity="number.weird")
            )
        self.assertEqual(result, 24.0)
