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


class TestUnsetIsACompleteNoOp(unittest.TestCase):
    """The actual #125 fix, proven directly: an install that has never
    configured solver_max_discharge_live_entity at all (the correct,
    default state for everyone except this repo's own reference
    household) can NEVER hit the accidental-entity-collision bug, because
    entity_exists()/ha_get() are provably never even called."""

    def test_missing_key_returns_the_static_config_value(self):
        result = solver_writer.resolve_max_discharge_kw(_cfg())
        self.assertEqual(result, 24.0)

    def test_missing_key_never_calls_entity_exists_or_ha_get(self):
        with (
            patch.object(solver_writer, "entity_exists") as mock_exists,
            patch.object(solver_writer, "ha_get") as mock_get,
        ):
            solver_writer.resolve_max_discharge_kw(_cfg())
            mock_exists.assert_not_called()
            mock_get.assert_not_called()

    def test_explicit_none_is_also_a_complete_no_op(self):
        with (
            patch.object(solver_writer, "entity_exists") as mock_exists,
            patch.object(solver_writer, "ha_get") as mock_get,
        ):
            result = solver_writer.resolve_max_discharge_kw(
                _cfg(solver_max_discharge_live_entity=None)
            )
            self.assertEqual(result, 24.0)
            mock_exists.assert_not_called()
            mock_get.assert_not_called()

    def test_empty_string_is_also_a_complete_no_op(self):
        # A real, plausible shape a config-flow field can end up with
        # (e.g. a stale/cleared UI submission) -- must not be treated as
        # "configured".
        with (
            patch.object(solver_writer, "entity_exists") as mock_exists,
            patch.object(solver_writer, "ha_get") as mock_get,
        ):
            result = solver_writer.resolve_max_discharge_kw(
                _cfg(solver_max_discharge_live_entity="")
            )
            self.assertEqual(result, 24.0)
            mock_exists.assert_not_called()
            mock_get.assert_not_called()


class TestMarksExactRealRepro(unittest.TestCase):
    """The precise, real numbers from Mark's own live issue report --
    reproduced faithfully via a genuinely CONFIGURED live entity (opt-in,
    matching this repo's own reference household's real, intentional
    setup), not the old always-on hardcoded behaviour."""

    def test_configured_entity_that_genuinely_exists_wins_over_static_config(self):
        with (
            patch.object(solver_writer, "entity_exists", return_value=True),
            patch.object(
                solver_writer,
                "ha_get",
                return_value={"attributes": {"max": 1.93}},
            ),
        ):
            result = solver_writer.resolve_max_discharge_kw(
                _cfg(
                    solver_max_discharge_live_entity=(
                        "number.logger_charging_discharging_power_kw"
                    )
                )
            )
        # This is the CORRECT behaviour once genuinely, explicitly
        # configured -- the mechanism is real and intentional for the
        # household that opts into it. The #125 bug was never "live
        # values shouldn't win" -- it was "this shouldn't be hardcoded
        # onto every install with no way to opt out."
        self.assertEqual(result, 1.93)

    def test_configured_entity_that_does_not_exist_falls_back_cleanly(self):
        with (
            patch.object(solver_writer, "entity_exists", return_value=False),
            patch.object(solver_writer, "ha_get") as mock_get,
        ):
            result = solver_writer.resolve_max_discharge_kw(
                _cfg(solver_max_discharge_live_entity="number.some_entity")
            )
            mock_get.assert_not_called()
        self.assertEqual(result, 24.0)


class TestMalformedLiveEntityDegradesGracefully(unittest.TestCase):
    """A configured live entity that exists but doesn't have a usable
    numeric 'max' attribute must never crash the solve -- same
    honest-fallback-over-crash discipline as every other real external
    read in this file."""

    def test_missing_max_attribute_key_falls_back(self):
        with (
            patch.object(solver_writer, "entity_exists", return_value=True),
            patch.object(solver_writer, "ha_get", return_value={"attributes": {}}),
        ):
            result = solver_writer.resolve_max_discharge_kw(
                _cfg(solver_max_discharge_live_entity="number.weird")
            )
        self.assertEqual(result, 24.0)

    def test_non_numeric_max_attribute_falls_back(self):
        with (
            patch.object(solver_writer, "entity_exists", return_value=True),
            patch.object(
                solver_writer,
                "ha_get",
                return_value={"attributes": {"max": "unknown"}},
            ),
        ):
            result = solver_writer.resolve_max_discharge_kw(
                _cfg(solver_max_discharge_live_entity="number.weird")
            )
        self.assertEqual(result, 24.0)

    def test_missing_attributes_key_entirely_falls_back(self):
        with (
            patch.object(solver_writer, "entity_exists", return_value=True),
            patch.object(solver_writer, "ha_get", return_value={}),
        ):
            result = solver_writer.resolve_max_discharge_kw(
                _cfg(solver_max_discharge_live_entity="number.weird")
            )
        self.assertEqual(result, 24.0)
