"""nimbus issue #1310: pre-refactor characterization coverage for the
battery-fleet extraction (PR #1308 / nimbus issue #1300).

## Why this test exists

PR #1308 moved build_extra_batteries() and _resolve_battery_participant_
history() out of solver_writer.py into solver_inputs/extra_batteries.py
and solver_inputs/battery_participants.py. #1300's own acceptance
criteria required a pre-move characterization test comparing complete
outputs byte-for-byte BEFORE any code moved; #1308 shipped by adapting
existing tests instead. CI passed and an independent review found no
logic differences, but the actual required evidence was never built.
This is that evidence, built after the fact against the pre-extraction
commit rather than skipped because the "before" moment has passed.

## Reference revision

    0d0d553a22e8e4d5db2339ede13337d1645fa220

the commit immediately before #1308 -- both target functions still live
directly on solver_writer.py at this commit (confirmed with `grep -n
"^def build_extra_batteries\\|^def _resolve_battery_participant_history"`
before capturing anything from it).

## Fixture inputs and reproduction command

tests/golden_1310/scenarios.py holds the exact fixture: two
battery_participant subentries ("ev_m3p" and "ev_my") sharing a 15 kW
garage charger, one with a departure deadline, one with a real
mid-morning away window -- see that module's own docstring for the full
scenario design and why each concrete value was chosen.
tests/golden_1310/reference_0d0d553a.json is that fixture's COMPLETE
output, captured from the reference revision above via
tests/golden_1310/capture.py, whose own module docstring is the exact
reproduction command (run from a `git worktree add --detach` checkout
of that commit, since the pre- and post-extraction layouts cannot both
exist in one working tree at once).

## What is compared, and why exact equality rather than a tolerance

Every dataclass field of every returned BatteryConfig (via
`dataclasses.fields()`, not a curated subset -- see
scenarios.serialize_battery_config()), both charge/discharge arrays in
full, final SoC, and the raw soc_hist this function carries out (nimbus
#949). All inputs (subentry data, recorder history, the scored day, the
timezone) are fixed literals in scenarios.py -- nothing here reads a
clock, a random source, or a real network/HA call, so there is no
legitimate source of nondeterminism between two runs of the SAME code.
Equality is therefore exact, not tolerant -- same posture as this
repo's own tests/test_golden_master.py, whose `canonical()` applies no
numeric tolerance either (only excludes a named wall-clock field this
fixture has no equivalent of). The one caveat golden_master's own
`_version_note()` also carries applies equally here: a float difference
IS possible across Python/numpy versions, since this fixture's own
`charge_efficiency`/`discharge_efficiency` fields are `** 0.5` (sqrt)
results -- CI pins the interpreter, so this is noted rather than
papered over with a fuzzy tolerance that would hide a real regression
as easily as a version drift.

## Proving this test actually catches a regression (not committed)

Per this repo's own convention for a new pinning test (see the
#1290/#1291 pinning tests' own verification in the worklog), before
opening the PR that adds this file, two real regressions were
introduced by hand against a scratch copy of solver_inputs/
battery_participants.py and confirmed to fail this test with a clear,
specific diff, then reverted:

  1. Reverting nimbus #1140's own fix in
     `_widen_shared_charger_cap_to_achieved()` (summing charge_kw alone
     instead of charge_kw + discharge_kw) -- this test's
     `test_resolve_history_matches_the_pre_extraction_reference` failed
     at `resolve_history[0].battery.shared_charger_max_kw: 20.0 != 15.0`
     (reference vs. the mutated code: with charge-only widening the
     achieved peak is 8.0 kW, which never exceeds the configured 15.0 kW
     cap, so the cap stays at 15.0 instead of correctly widening to
     20.0) -- the exact #1140 regression this fixture exists to catch.
  2. Reversing the order of the two battery_participant subentries
     passed into `build_extra_batteries_native_hass()`'s own subentry
     list -- failed at `build_extra_batteries[0].name`/`.available`
     ('ev_my'/False where the reference has 'ev_m3p'/True), confirming
     participant ORDER is pinned, not just membership.

Neither mutation is left in the tree; this docstring is the record that
they were made and both failed the way a real regression should.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import _solver_path  # noqa: F401
import solver_writer
from golden_1310 import scenarios
from solver_inputs import battery_participants as battery_participants_inputs
from solver_inputs import extra_batteries as extra_batteries_inputs

REFERENCE_PATH = Path(__file__).parent / "golden_1310" / "reference_0d0d553a.json"


def _first_difference(a: Any, b: Any, path: str = "$") -> str | None:
    """Same shape as tests/test_golden_master.py's own helper -- reports
    exactly where two nested JSON-safe structures first disagree, rather
    than a bare assertEqual's own unreadable full-structure diff."""
    if type(a) is not type(b):
        return f"{path}: {type(a).__name__} {a!r:.200} != {type(b).__name__} {b!r:.200}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: present on one side only"
            d = _first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            d = _first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r:.200} != {b!r:.200}"


class TestBatteryFleetCharacterization(unittest.TestCase):
    """Complete-output comparison: current solver_inputs implementation
    vs. the pre-extraction (commit 0d0d553a) reference capture."""

    @classmethod
    def setUpClass(cls):
        with open(REFERENCE_PATH, encoding="utf-8") as f:
            cls.reference = json.load(f)
        cls.reference.pop("_layout", None)
        cls.got = scenarios.capture(
            solver_writer,
            extra_batteries_inputs.build_extra_batteries,
            battery_participants_inputs._resolve_battery_participant_history,
        )

    def test_build_extra_batteries_matches_the_pre_extraction_reference(self):
        diff = _first_difference(
            self.reference["build_extra_batteries"], self.got["build_extra_batteries"]
        )
        self.assertIsNone(
            diff,
            "build_extra_batteries() output diverged from the pre-extraction "
            f"reference (commit 0d0d553a) at {diff} -- regenerate the reference "
            "per capture.py's own docstring only if this divergence is an "
            "INTENTIONAL behavior change, never to make a real regression "
            "disappear.",
        )

    def test_resolve_history_matches_the_pre_extraction_reference(self):
        diff = _first_difference(
            self.reference["resolve_history"], self.got["resolve_history"]
        )
        self.assertIsNone(
            diff,
            "_resolve_battery_participant_history() output diverged from the "
            f"pre-extraction reference (commit 0d0d553a) at {diff} -- see the "
            "note on test_build_extra_batteries_matches... above.",
        )

    # -- Spot checks below are documentation, not the authority: the two
    # tests above (complete-output equality against the pre-extraction
    # reference) are what #1310 actually asks for. These exist only so a
    # reader doesn't have to parse the JSON fixture to see what it
    # demonstrates -- they duplicate a subset of what the reference
    # comparison already pins.

    def test_two_ordered_participants_with_complete_fields(self):
        built = self.got["build_extra_batteries"]
        self.assertEqual([b["name"] for b in built], ["ev_m3p", "ev_my"])
        for b in built:
            for field in (
                "capacity_kwh",
                "initial_soc_kwh",
                "min_soc_kwh",
                "max_soc_kwh",
                "max_charge_kw",
                "max_discharge_kw",
                "charge_efficiency",
                "discharge_efficiency",
                "shared_charger_group",
                "shared_charger_max_kw",
            ):
                self.assertIn(field, b)

    def test_departure_deadline_reaches_both_functions(self):
        """ev_m3p's departure_hour/must_have_soc_by_departure_percent pair
        (#1111) resolves to a real period index on BOTH the live path
        (build_extra_batteries -- configured 48.0 kWh target at period 7,
        unchanged) and the history path
        (_resolve_battery_participant_history -- widened DOWN to 21.0
        kWh, the day's real initial SoC, since the day's own reconstructed
        trajectory never approached the configured target by period 0)."""
        built = {b["name"]: b for b in self.got["build_extra_batteries"]}
        self.assertEqual(built["ev_m3p"]["must_have_soc_by_period_index"], 7)
        self.assertEqual(built["ev_m3p"]["must_have_soc_kwh"], 48.0)

        history = {
            r["battery"]["name"]: r["battery"] for r in self.got["resolve_history"]
        }
        self.assertEqual(history["ev_m3p"]["must_have_soc_by_period_index"], 0)
        self.assertEqual(history["ev_m3p"]["must_have_soc_kwh"], 21.0)

    def test_availability_gating_zeros_a_real_nonzero_reading(self):
        """ev_my's real away window (hours 9-11) must zero its own real
        3 kW discharge reading at hour 10 -- not just mask an
        already-zero period, which would be a vacuous check (nimbus
        #467/#768)."""
        history = {r["battery"]["name"]: r for r in self.got["resolve_history"]}
        row = history["ev_my"]
        self.assertEqual(row["battery"]["unavailable_period_indices"], [9, 10, 11])
        self.assertEqual(row["actual_discharge_kw"][10], 0.0)
        self.assertEqual(row["actual_charge_kw"][10], 0.0)

    def test_shared_charger_widening_includes_discharge_not_just_charge(self):
        """nimbus #1109 widens the configured 15 kW garage cap to what
        the group actually drew; nimbus #1140 fixed the widening to sum
        charge+discharge (matching the LP constraint it widens against)
        rather than charge alone. At hour 18, ev_m3p charges 8 kW and
        ev_my discharges 12 kW through the SAME shared charger -- a
        combined draw of 20 kW. If #1140 ever regressed (charge-only
        widening), this would read 8.0, not 20.0 -- confirmed by
        deliberately reverting that fix locally while writing this test
        (see this module's own docstring)."""
        history = {
            r["battery"]["name"]: r["battery"] for r in self.got["resolve_history"]
        }
        self.assertEqual(history["ev_m3p"]["shared_charger_max_kw"], 20.0)
        self.assertEqual(history["ev_my"]["shared_charger_max_kw"], 20.0)


if __name__ == "__main__":
    unittest.main()
