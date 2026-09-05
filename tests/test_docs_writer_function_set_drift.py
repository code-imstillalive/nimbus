"""Regression test for nimbus issue #357 (Mark Purcell, codebase review):
the standalone/cron writer script (docs/real-world-integration/files/
nimbus_solver_forecast_writer.py) and the integration copy
(custom_components/nimbus_load/solver_writer.py) are two independently-
maintained files with real, substantial content differences -- nothing
enforced that those differences stay INTENTIONAL rather than a missed bug
fix silently absent from one copy.

The real, concrete example this issue was reopened over: the #370/#374
zero-load-fallback startup-race guard (_is_transient_startup_load_
forecast_error) and compute_forecast_coverage_hours were both fixed on
the native runtime weeks before Mark's own live health-check found the
standalone copy still shipping the pre-#370 behaviour verbatim -- a real
household running this exact script via cron would have hit the same
confidently-wrong "status: optimal" plan #370 already fixed once. Both
are ported as of this same change (2026-09-05); this test is what stops
the NEXT one from silently drifting the same way.

Deliberately does NOT try to diff function BODIES, only top-level
`def`/`async def` NAMES -- catching "a function silently vanished from
one copy" is the real, cheap, high-value signal; genuine per-line drift
inside a function both files legitimately share is a human-review
question this test was never meant to answer.

Three explicit lists, not one -- because "intentional, permanent
difference" and "known, still-open gap this issue tracks" are honestly
different things, and collapsing them would misrepresent which of these
is actually finished work:

- INTENTIONAL_NATIVE_ONLY: real execution-context differences confirmed
  by Mark's own triage on this issue -- native-HA-only entity
  registration/reporting/counterfactual functions that make no sense for
  a bare standalone script. Permanent, not tracked as a gap.
- INTENTIONAL_CRON_ONLY: the mirror case -- standalone-cron-only
  functions (the #251 phase-alignment fix, deliberately scoped to the
  one deployment mode that needs it since the native runtime gets an
  equivalent fix through a different mechanism). Permanent.
- KNOWN_OPEN_DRIFT: real, shared LOGIC that has NOT been ported yet as
  of this pass -- Mark's own "the ones that are shared logic and have
  already drifted or will" list, plus a same-day fresh diff finding one
  more (`_hour_in_schedule_block`) and a same-named-but-renamed pair
  (`scheduled_discharge_cost_rate`/`scheduled_salvage_value_rate` in the
  integration copy vs. `battery_discharge_cost_rate`/
  `battery_salvage_value_rate` in the docs copy -- an OLDER naming that
  never got renamed to match, not confirmed to be behaviourally
  identical). This list is real follow-up work for #357, expected to
  shrink over time as each entry gets ported (or confirmed to already be
  equivalent under its old name and removed from the list) -- shrinking
  it is a genuine fix; growing it silently is exactly what this test
  exists to prevent.

Any function that shows up in exactly one file's own top-level def set
and is NOT accounted for in one of these three lists fails this test --
that is the actual enforcement mechanism Mark asked for: the next
missing fix fails CI instead of waiting for someone to notice a chart.
"""

from __future__ import annotations

import ast
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_INTEGRATION_PATH = os.path.join(
    _REPO_ROOT, "custom_components", "nimbus_load", "solver_writer.py"
)
_DOCS_PATH = os.path.join(
    _REPO_ROOT,
    "docs",
    "real-world-integration",
    "files",
    "nimbus_solver_forecast_writer.py",
)

# Native-HA-only: entity registration, reporting/counterfactual publish
# functions, and the two small native-mode-only helpers -- confirmed by
# Mark Purcell's own triage on issue #357 to be genuine execution-context
# differences, not missing bug fixes. A bare standalone/cron script has
# no HA entity registry to register handlers against, and nothing to
# publish these reports TO outside of a real running HA instance.
INTENTIONAL_NATIVE_ONLY = frozenset(
    {
        "_compute_report_for_window",
        "register_entity_handler",
        "unregister_entity_handler",
        "ha_call_service_with_response",
        "compute_daily_quality_report",
        "compute_efficiency_backtest_report",
        "compute_nimbus_only_soc_counterfactual",
        "publish_daily_quality_report",
        "publish_efficiency_backtest_report",
        "publish_nimbus_only_soc_counterfactual",
        "publish_weather_forecast_mirrors",
        "resolve_real_entity_id",
        "_load_token",
    }
)

# Standalone-cron-only: the #251 phase-alignment fix. The native runtime
# gets an equivalent fix through #244/#247's own mechanism (a phase-
# aligned async_track_utc_time_change), so this function has never had
# an integration-copy counterpart to drift against -- see this
# function's own test file, tests/test_settlement_capture_timing.py.
INTENTIONAL_CRON_ONLY = frozenset({"seconds_to_settlement_capture"})

# Real, shared LOGIC (not HA plumbing) that has NOT been ported to the
# docs copy yet, as of this pass -- tracked, open follow-up work for
# #357, not permanent. Each entry here is a real gap; this list is
# expected to shrink, one porting PR at a time, not grow silently.
KNOWN_OPEN_DRIFT_INTEGRATION_ONLY = frozenset(
    {
        "_hour_in_schedule_block",
        "_kw_scale_factor",
        "_load_solar_delivery_state",
        "_save_solar_delivery_state",
        "_log_active_household_specific_overrides_once",
        "_safe_fromisoformat",
        "blend_price_with_secondary_sources",
        "compute_cost_band",
        "compute_cost_breakdown",
        "fetch_entity_history_range",
        "resample_generic_price_forecast_with_coverage",
        "resample_history_nearest",
        "resolve_load_forecast_source_label",
        "update_solar_delivery_ratio",
        # Renamed in the integration copy (2026-08-2x era) to
        # scheduled_discharge_cost_rate/scheduled_salvage_value_rate --
        # the docs copy still carries the pre-rename names below under
        # KNOWN_OPEN_DRIFT_DOCS_ONLY, not confirmed to be behaviourally
        # identical to the renamed versions (a real thing to check when
        # this entry is next picked up, not just a mechanical rename).
        "scheduled_discharge_cost_rate",
        "scheduled_salvage_value_rate",
    }
)
KNOWN_OPEN_DRIFT_DOCS_ONLY = frozenset(
    {
        "battery_discharge_cost_rate",
        "battery_salvage_value_rate",
    }
)


def _top_level_def_names(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class TestDocsWriterFunctionSetDoesNotSilentlyDrift(unittest.TestCase):
    def setUp(self):
        self.integration_defs = _top_level_def_names(_INTEGRATION_PATH)
        self.docs_defs = _top_level_def_names(_DOCS_PATH)

    def test_both_files_have_a_meaningful_number_of_functions(self):
        """Sanity check the AST walk itself found real content, not an
        empty/misparsed file -- a silent 0 on either side would make
        every other assertion in this test vacuously pass."""
        self.assertGreater(len(self.integration_defs), 40)
        self.assertGreater(len(self.docs_defs), 40)

    def test_integration_only_functions_are_all_accounted_for(self):
        integration_only = self.integration_defs - self.docs_defs
        unaccounted = (
            integration_only
            - INTENTIONAL_NATIVE_ONLY
            - KNOWN_OPEN_DRIFT_INTEGRATION_ONLY
        )
        self.assertEqual(
            unaccounted,
            set(),
            f"{sorted(unaccounted)} exist in the integration copy "
            f"(custom_components/nimbus_load/solver_writer.py) but NOT in "
            f"the standalone/cron copy (docs/real-world-integration/files/"
            f"nimbus_solver_forecast_writer.py), and aren't in this test's "
            f"own INTENTIONAL_NATIVE_ONLY or KNOWN_OPEN_DRIFT_INTEGRATION_"
            f"ONLY lists -- this is exactly the #357 drift class (a fix "
            f"landed on the native runtime and never got ported). Either "
            f"port the function to the docs copy, or -- only if it's a "
            f"genuine, permanent native-HA-only concern -- add it to "
            f"INTENTIONAL_NATIVE_ONLY with a one-line reason.",
        )

    def test_docs_only_functions_are_all_accounted_for(self):
        docs_only = self.docs_defs - self.integration_defs
        unaccounted = docs_only - INTENTIONAL_CRON_ONLY - KNOWN_OPEN_DRIFT_DOCS_ONLY
        self.assertEqual(
            unaccounted,
            set(),
            f"{sorted(unaccounted)} exist in the standalone/cron copy but "
            f"NOT in the integration copy, and aren't in this test's own "
            f"INTENTIONAL_CRON_ONLY or KNOWN_OPEN_DRIFT_DOCS_ONLY lists -- "
            f"a genuinely new standalone-only function needs a one-line "
            f"reason added to INTENTIONAL_CRON_ONLY (if permanent) or "
            f"KNOWN_OPEN_DRIFT_DOCS_ONLY (if it should eventually be "
            f"ported/reconciled with the integration copy).",
        )

    def test_known_open_drift_lists_dont_silently_go_stale(self):
        """If a KNOWN_OPEN_DRIFT entry gets ported (appears in BOTH files
        now) or renamed/removed (appears in NEITHER), the list itself
        must be updated in the same PR -- a stale entry here would let a
        *different*, unrelated function silently reuse its "slot" without
        anyone noticing. Fails loudly rather than silently underenforcing."""
        for name in KNOWN_OPEN_DRIFT_INTEGRATION_ONLY:
            with self.subTest(name=name):
                self.assertIn(
                    name,
                    self.integration_defs,
                    f"{name!r} is listed in KNOWN_OPEN_DRIFT_INTEGRATION_ONLY "
                    f"but no longer exists in the integration copy -- remove "
                    f"it from the list (renamed, deleted, or ported and the "
                    f"integration side changed name too).",
                )
                self.assertNotIn(
                    name,
                    self.docs_defs,
                    f"{name!r} is listed in KNOWN_OPEN_DRIFT_INTEGRATION_ONLY "
                    f"but now exists in BOTH files -- it's been ported. "
                    f"Remove it from this list, that's a real fix worth "
                    f"recording as done, not leaving the list stale.",
                )
        for name in KNOWN_OPEN_DRIFT_DOCS_ONLY:
            with self.subTest(name=name):
                self.assertIn(
                    name,
                    self.docs_defs,
                    f"{name!r} is listed in KNOWN_OPEN_DRIFT_DOCS_ONLY but "
                    f"no longer exists in the docs copy -- remove it from "
                    f"the list.",
                )
                self.assertNotIn(
                    name,
                    self.integration_defs,
                    f"{name!r} is listed in KNOWN_OPEN_DRIFT_DOCS_ONLY but "
                    f"now exists in BOTH files -- reconciled/ported. Remove "
                    f"it from this list.",
                )


if __name__ == "__main__":
    unittest.main()
