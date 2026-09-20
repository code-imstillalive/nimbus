"""nimbus issue #1149 follow-up: a rescore must move the headline-only
attributes too, not just the five history-row fields.

`rescore_quality_history()` keeps the headline in sync with the rescored
row by copying `_QUALITY_HISTORY_FIELDS` -- epr, j_ref, j_ach, j_star,
regret_dollars. `energy_decomposition` is deliberately NOT in that tuple
(a history row has to stay small enough that a year of them fits in one
attribute payload), so it was computed on every rescore and then
silently dropped.

Found by running a real rescore on a dev install rather than by review:
the state and the five fields moved to the rescored day's figures while
`energy_decomposition` still described the PREVIOUS computation. That is
exactly the defect #1120 exists to fix -- "correcting one and not the
other would leave them disagreeing" -- reappearing on a field that
postdates the rescore path and so was never part of its sync.

Same stubbed-dependency pattern as test_1120_rescore_history_persists.py.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
NOW = datetime(2026, 9, 20, 6, 30, tzinfo=BRISBANE)

STALE_DECOMP = {
    "reference": {
        "charge_kwh": 0.0,
        "discharge_kwh": 0.0,
        "grid_import_kwh": 1.0,
        "grid_export_kwh": 0.0,
    },
    "achieved": {
        "charge_kwh": 1.0,
        "discharge_kwh": 1.0,
        "grid_import_kwh": 1.0,
        "grid_export_kwh": 1.0,
    },
    "oracle": {
        "charge_kwh": 1.0,
        "discharge_kwh": 1.0,
        "grid_import_kwh": 1.0,
        "grid_export_kwh": 1.0,
    },
    "achieved_minus_oracle": {
        "charge_kwh": 0.0,
        "discharge_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "grid_export_kwh": 0.0,
    },
}

FRESH_DECOMP = {
    "reference": {
        "charge_kwh": 0.0,
        "discharge_kwh": 0.0,
        "grid_import_kwh": 48.0,
        "grid_export_kwh": 0.0,
    },
    "achieved": {
        "charge_kwh": 108.12,
        "discharge_kwh": 103.1,
        "grid_import_kwh": 60.01,
        "grid_export_kwh": 89.72,
    },
    "oracle": {
        "charge_kwh": 83.14,
        "discharge_kwh": 88.75,
        "grid_import_kwh": 39.83,
        "grid_export_kwh": 80.17,
    },
    "achieved_minus_oracle": {
        "charge_kwh": 24.98,
        "discharge_kwh": 14.35,
        "grid_import_kwh": 20.18,
        "grid_export_kwh": 9.55,
    },
}


def _entry(epr, decomp):
    return {
        "epr": epr,
        "epr_pct": round(epr * 100.0, 2),
        "j_ref": 6.0,
        "j_ach": -24.0,
        "j_star": -25.0,
        "regret_dollars": 1.0,
        "energy_decomposition": decomp,
    }


def _run(days, existing, side_effect, version="0.94.398"):
    posted = {}

    def _post(entity_id, state, attributes):
        posted["state"] = state
        posted["attributes"] = attributes

    with (
        patch.object(solver_writer, "ha_get", return_value=existing),
        patch.object(solver_writer, "ha_post_state", side_effect=_post),
        patch.object(solver_writer, "_nimbus_version", return_value=version),
        patch.object(
            solver_writer, "_compute_report_for_window", side_effect=side_effect
        ),
    ):
        result = solver_writer.rescore_quality_history({}, NOW, days)
    return result, posted


class TestRescoringThePublishedDayMovesTheDecomposition(unittest.TestCase):
    def test_the_headline_decomposition_is_replaced_not_left_stale(self):
        existing = {
            "state": "106.43",
            "attributes": {
                "latest_date": "2026-09-19",
                "history": {"2026-09-19": {"epr": 1.064}},
                "energy_decomposition": STALE_DECOMP,
            },
        }
        _, posted = _run(1, existing, lambda *a, **k: _entry(0.9481, FRESH_DECOMP))

        self.assertEqual(posted["attributes"]["energy_decomposition"], FRESH_DECOMP)
        self.assertNotEqual(posted["attributes"]["energy_decomposition"], STALE_DECOMP)

    def test_it_moves_together_with_the_state_and_the_history_fields(self):
        # The whole point is that these stay consistent -- a rescored
        # state beside a decomposition of the previous computation is
        # the defect, so all three are asserted in one place.
        existing = {
            "state": "106.43",
            "attributes": {
                "latest_date": "2026-09-19",
                "history": {"2026-09-19": {"epr": 1.064}},
                "energy_decomposition": STALE_DECOMP,
            },
        }
        _, posted = _run(1, existing, lambda *a, **k: _entry(0.9481, FRESH_DECOMP))

        self.assertEqual(posted["state"], 94.81)
        self.assertAlmostEqual(posted["attributes"]["epr"], 0.9481)
        self.assertEqual(
            posted["attributes"]["energy_decomposition"]["achieved_minus_oracle"][
                "charge_kwh"
            ],
            24.98,
        )


class TestRescoringAnOlderDayLeavesTheHeadlineAlone(unittest.TestCase):
    """Symmetry with the existing #1120 behaviour: the headline only
    moves when the rescored day IS the published day. Rescoring an older
    row must not overwrite today's decomposition with that older day's."""

    def test_an_older_days_decomposition_does_not_become_the_headline(self):
        existing = {
            "state": "106.43",
            "attributes": {
                "latest_date": "2026-09-19",
                "history": {"2026-09-17": {"epr": 0.2}},
                "energy_decomposition": STALE_DECOMP,
            },
        }

        def _side(cfg, start, end, allow_partial):
            if start.date().isoformat() == "2026-09-17":
                return _entry(0.5, FRESH_DECOMP)
            return None

        _, posted = _run(3, existing, _side)

        self.assertEqual(posted["state"], "106.43", "headline must not move")
        self.assertEqual(
            posted["attributes"]["energy_decomposition"],
            STALE_DECOMP,
            "an older day's decomposition must not become the headline's",
        )


class TestAnEntryWithoutTheFieldIsSurvivable(unittest.TestCase):
    """A report computed before this field existed, or one where the
    decomposition could not be built, must not blank an existing
    headline value or raise."""

    def test_a_missing_field_leaves_whatever_was_there(self):
        existing = {
            "state": "106.43",
            "attributes": {
                "latest_date": "2026-09-19",
                "history": {"2026-09-19": {"epr": 1.064}},
                "energy_decomposition": STALE_DECOMP,
            },
        }
        entry = _entry(0.9481, FRESH_DECOMP)
        del entry["energy_decomposition"]

        _, posted = _run(1, existing, lambda *a, **k: entry)

        self.assertEqual(posted["attributes"]["energy_decomposition"], STALE_DECOMP)
        self.assertAlmostEqual(posted["attributes"]["epr"], 0.9481)

    def test_an_install_that_never_had_the_field_does_not_gain_an_empty_one(self):
        existing = {
            "state": "106.43",
            "attributes": {
                "latest_date": "2026-09-19",
                "history": {"2026-09-19": {"epr": 1.064}},
            },
        }
        entry = _entry(0.9481, FRESH_DECOMP)
        del entry["energy_decomposition"]

        _, posted = _run(1, existing, lambda *a, **k: entry)
        self.assertNotIn("energy_decomposition", posted["attributes"])


if __name__ == "__main__":
    unittest.main()
