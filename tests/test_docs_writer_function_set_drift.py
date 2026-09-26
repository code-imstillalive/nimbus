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

Four explicit lists, not one -- because "intentional, permanent
difference", "known, still-open gap this issue tracks", and "artifact of
an in-progress refactor" are honestly different things, and collapsing
them would misrepresent which of these is actually finished work:

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
- INTENTIONAL_EXTRACTED_FROM_MAIN: functions that are top-level in the
  integration only because nimbus issue #735 pulled them out of main(),
  while the docs/cron copy still has the same logic inline in its own
  main(). Nothing is missing from either copy, so this is not tracked
  work -- but it is not a permanent execution-context difference either,
  which is why it is not folded into INTENTIONAL_NATIVE_ONLY. Expected
  to grow as #735's remaining stages land.

Any function that shows up in exactly one side's own top-level def set
and is NOT accounted for in one of these four lists fails this test --
that is the actual enforcement mechanism Mark asked for: the next
missing fix fails CI instead of waiting for someone to notice a chart.

"One side's" rather than "one file's": the integration side is a UNION
across solver_writer.py and every module #735 has extracted out of it
(see _integration_paths() below for why, and for the two concrete ways
a single-file comparison would report the refactor as drift -- one of
them by forcing a factually false annotation into this file).
"""

from __future__ import annotations

import ast
import glob
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_NIMBUS_DIR = os.path.join(_REPO_ROOT, "custom_components", "nimbus_load")
_INTEGRATION_PATH = os.path.join(_NIMBUS_DIR, "solver_writer.py")
_DOCS_PATH = os.path.join(
    _REPO_ROOT,
    "docs",
    "real-world-integration",
    "files",
    "nimbus_solver_forecast_writer.py",
)

# nimbus issue #735: the integration side is no longer ONE file.
#
# The docs/cron copy is a single standalone script and always will be --
# that is the whole point of it. The integration copy is being split into
# a real module structure, one slice at a time (#735 stage 1 moved solar
# input gathering into solver_inputs/solar.py). Comparing the docs script
# against solver_writer.py ALONE would make that split look like drift,
# and in two genuinely wrong ways:
#
# 1. A function listed in KNOWN_OPEN_DRIFT_INTEGRATION_ONLY that gets
#    relocated would vanish from the integration's def set and trip
#    test_known_open_drift_lists_dont_silently_go_stale -- reporting a
#    real, still-unported gap as if it had been resolved.
# 2. Worse: a function present in BOTH copies (e.g. fetch_price_history,
#    fetch_p2p_fixed_export_kw, both slated to move in #735 stage 2)
#    would become "docs-only" and have to be added to KNOWN_OPEN_DRIFT_
#    DOCS_ONLY -- an annotation asserting the integration is MISSING a
#    fix it demonstrably still has. The test would be forcing a factually
#    false statement into the codebase.
#
# So the integration's function surface is the UNION across the writer
# and every module #735 extracts out of it. That keeps #357's actual
# guarantee exactly as strong -- "this logic exists on the native runtime
# but not in the standalone copy" is still caught, because relocating a
# function inside the integration doesn't remove it from the union --
# while letting the refactor proceed without either false annotation.
#
# nimbus issue #952 (Mark Purcell, 48-hour IV&V #950): this tuple listed
# `solver_inputs/*.py` ALONE, so `solver_publish.py` -- extracted from
# `main()` by the same #735 refactor (PR #888, hoisting
# `publish_household_load_total_forecast()`) but landing directly under
# `nimbus_load/` rather than inside a package -- was invisible here. A
# fix applied only inside it and never ported to the standalone copy
# would have passed this entire suite silently, which is exactly the
# regression class #357 exists to catch. No live drift had resulted yet;
# it was an unguarded blind spot, not a gap.
#
# Registered explicitly rather than by widening this to a
# `solver_*.py` glob. That glob would also sweep in `solver_runtime.py`,
# which is HA-scheduling machinery with no standalone counterpart at
# all -- adding its defs to the integration union could make a genuinely
# docs-only entry look present. The union must track what #735
# *extracted from main()*, not everything whose filename happens to
# start with "solver".
#
# `test_a_new_top_level_extraction_cannot_go_unregistered` below is the
# durable half of this fix: it fails when the NEXT extraction lands
# without being added here, so #952 cannot recur silently for stages
# 2-4.
_EXTRACTED_PACKAGE_GLOBS = (
    os.path.join(_NIMBUS_DIR, "solver_inputs", "*.py"),
    os.path.join(_NIMBUS_DIR, "solver_publish.py"),
)


def _integration_paths() -> list[str]:
    paths = [_INTEGRATION_PATH]
    for pattern in _EXTRACTED_PACKAGE_GLOBS:
        for path in sorted(glob.glob(pattern)):
            if os.path.basename(path) == "__init__.py":
                # Package docstring/re-exports only, no solve logic of its
                # own -- nothing here is a candidate for porting.
                continue
            paths.append(path)
    return paths


# Native-HA-only: entity registration, reporting/counterfactual publish
# functions, and the two small native-mode-only helpers -- confirmed by
# Mark Purcell's own triage on issue #357 to be genuine execution-context
# differences, not missing bug fixes. A bare standalone/cron script has
# no HA entity registry to register handlers against, and nothing to
# publish these reports TO outside of a real running HA instance.
INTENTIONAL_NATIVE_ONLY = frozenset(
    {
        "_compute_report_for_window",
        # nimbus issue #363 step 2 (Mark Purcell's own approved staged-
        # extraction plan): a pure code-organization move out of main()
        # in the integration copy only -- the docs/cron copy's own
        # main() still has the equivalent forecast-dict/attrs-building
        # logic inline, unrefactored. Not a missing bug fix; porting
        # this exact split to the standalone script is a separate,
        # not-yet-scoped follow-up.
        "publish_plan",
        # nimbus issue #563 item 5: called only from publish_plan()
        # itself (already exempted above, same reasoning) -- and its
        # own real value (a multi-participant breakdown) has nothing to
        # show in standalone/cron mode anyway, since battery_participant
        # subentries are themselves native-HA-only (build_extra_
        # batteries() returns [] there, so plan.batteries is always a
        # single-element ["home"] list).
        "build_per_battery_forecast",
        "register_entity_handler",
        "unregister_entity_handler",
        "ha_call_service_with_response",
        "compute_daily_quality_report",
        # nimbus #994: builds the quality report's own `history` table.
        # Native-only for the same reason its two callers directly above
        # and below already are -- the standalone/cron copy has no
        # quality-report publisher at all, so there is nothing there for
        # this to carry forward.
        "_carry_forward_quality_history",
        # nimbus #1248: whether that helper's recovery cache is in play at all.
        # Native-only in the most literal sense available -- it returns
        # `_NATIVE_HASS is not None`, so in the standalone/cron copy it would be
        # a function that can only ever return False. The cache it gates is
        # process-lifetime, and a cron writer runs once per invocation and
        # exits, so it is empty on every run and could never rescue anything
        # there. Porting it would be porting dead code.
        "_quality_history_cache_active",
        # nimbus #1120: reads this package's own manifest.json so a
        # frozen history row can say which release scored it. Called
        # only from _carry_forward_quality_history() directly above, so
        # it is native-only for exactly that helper's reason -- this
        # drift check compares against the standalone/cron FORECAST
        # writer, which has no quality-report publisher. The cron QUALITY
        # writer, which does, carries its own copy of this helper and the
        # same _QUALITY_HISTORY_VERSION_FIELD; see
        # tests/test_1120_quality_history_version_stamp.py's own
        # TestTheCronCopyStampsToo, which pins that port.
        "_nimbus_version",
        # nimbus #1256: `{"nimbus_version": <release>}` or `{}`, splatted into
        # the four daily publishers' attributes so the stamp travels with the
        # computation instead of being supplied at render time by the entity's
        # own running-install fallback.
        #
        # Native-only because the DEFECT is native-only, not merely because
        # these publishers are. The failure needs an entity layer: #972's
        # `extra_state_attributes` re-injects `self._sw_version` when the
        # restored attributes carry no stamp, so a restart relabels old figures
        # with the new release. The standalone/cron writers post straight to
        # the States API with no entity object at all -- there is nothing there
        # to re-inject a running version, so there is nothing to override. The
        # cron quality writer freezes its own per-row stamp via
        # _QUALITY_HISTORY_VERSION_FIELD already (see #1120's own
        # TestTheCronCopyStampsToo).
        "_version_stamp",
        # nimbus #1082: decides whether an already-published score for a
        # day still stands, or was taken before that day's settlement
        # landed and should be scored again. Native-only for the identical
        # reason as the helper directly above -- the standalone/cron copy
        # has no quality-report publisher, so it has no published score to
        # reconsider.
        "_keep_published_quality_score",
        # nimbus issue #1200: the three halves of the provisional-row
        # repair. Native-only for the SAME reason as
        # _keep_published_quality_score directly above -- this check
        # compares against the standalone/cron FORECAST writer, which has
        # no quality-report publisher at all, so it has no history table
        # to flag and nothing to repair.
        #
        # The cron QUALITY writer is a different file and a real question,
        # so it is answered rather than left implied: it carries
        # _QUALITY_HISTORY_VERSION_FIELD ("v") and nothing else, and
        # #1162's own per-row reliability field ("r") was never ported to
        # it either -- see _epr_reliability_code below, already listed
        # here on that basis. #1200's "p" field follows that established
        # precedent deliberately, not by oversight: the repair sweep is
        # driven from the native solve cycle, which the cron writer does
        # not have, so a flag written there would be a row nothing ever
        # comes back for.
        "_settlement_is_provisional",
        "_settlement_entry_exists",
        "repair_provisional_quality_history",
        # nimbus issue #427: a private helper called only by
        # compute_daily_quality_report itself (already listed above) --
        # same native-only reasoning applies, not a separate gap.
        "_soc_discrepancy_stats",
        # nimbus issue #956: two private helpers of the same native-only
        # quality report -- _achieved_feasibility_stats is called only
        # from _compute_report_for_window, and _epr_reliability only
        # from the report dict it builds. Same reasoning as
        # _soc_discrepancy_stats directly above, which they sit beside
        # in the report: the standalone/cron script computes no quality
        # report at all, so there is nothing for these to be missing
        # FROM. Not a gap.
        "_achieved_feasibility_stats",
        "_epr_reliability",
        # nimbus issue #1162: the third helper of that same trio --
        # supplies `epr_reason` for the one reliability signal that had
        # none. Called only from _compute_report_for_window, beside the
        # two directly above, and native-only for the identical reason.
        "_epr_soc_reason",
        # nimbus issue #1162 (ask 3): what share of the published
        # regret is a pricing-path disagreement rather than a dispatch
        # one. Another private helper of the same native-only quality
        # report, called only from _compute_report_for_window.
        "_regret_path_delta_share",
        # nimbus issue #1162 ask 2: the one-character reliability verdict
        # stamped into each history row. Derives from the same three
        # signals as _epr_reliability() and is called only from
        # _carry_forward_quality_history -- same native-only reasoning as
        # its neighbours, since the standalone/cron writer maintains no
        # quality-report history table to stamp.
        "_epr_reliability_code",
        # nimbus issue #984: same again -- a private helper of the same
        # native-only quality report, measuring how much of a day the
        # recorder actually returned before that day may be scored. The
        # standalone/cron script computes no quality report, so there is
        # nothing for it to be missing from.
        "_history_coverage_hours",
        # nimbus issue #1054: the per-series half of the line directly
        # above -- _history_coverage_hours() now delegates to it so the
        # skip line can name WHICH sensor was short instead of only the
        # minimum. Not a separate concern from its own caller: same
        # native-only quality report, same "the standalone/cron script
        # computes no quality report, so there is nothing for it to be
        # missing from" reasoning.
        "_history_coverage_by_series",
        # nimbus issue #1012: a per-battery energy-balance check --
        # does each battery's measured throughput reconcile with its
        # measured SoC swing, and what efficiency would close the gap.
        # Called only from _compute_report_for_window() (already listed
        # above), so the same native-only reasoning as every other
        # quality-report helper here: the standalone/cron FORECAST
        # script computes no quality report, so there is nothing for it
        # to be missing from. (The separate cron QUALITY writer does
        # compute one and is not covered by this test's comparison --
        # wiring this there is tracked on #1012 itself.)
        "battery_energy_balance",
        # nimbus issue #1073 (Mark Purcell): battery_energy_balance()'s
        # own "is this window genuinely two-directional" test, called
        # from nowhere else. Inherits that function's native-only
        # reasoning exactly -- a helper cannot be a drift gap in a copy
        # that does not have the function it serves.
        "_is_mixed_direction_window",
        # nimbus #1086: battery_energy_balance()'s own "did this window
        # return to its starting SoC" test, which is what makes energy in
        # and energy out directly comparable. Same native-only reasoning
        # as its two neighbours above -- the cron FORECAST copy has no
        # quality-report publisher for it to serve.
        "_is_closed_soc_loop",
        # nimbus issue #428: only called from compute_daily_quality_
        # report's own solar/load/battery resampling (already listed
        # above) -- same native-only reasoning, not a separate gap.
        "resample_history_mean",
        # nimbus issue #919: called only from _compute_report_for_window
        # (already listed above), and it reads back the recorded STATE
        # HISTORY of a Nimbus-published sensor -- which only exists on a
        # real HA install with a recorder. A standalone/cron script has
        # no recorder to read a forecast trail out of at all, so this is
        # a genuine execution-context difference, not a missing fix.
        "_load_nowcast_skill_attributes",
        # nimbus issue #937 stage 3: the DAY-AHEAD forecast-regret
        # decomposition, published on the quality report beside #919's
        # one-step-ahead nowcast skill above. Native-only for a stronger
        # reason than its neighbour, and worth stating rather than
        # inheriting: it reads a snapshot out of an HA `Store` (via the
        # sync cache `solver_runtime` primes on the event loop before
        # dispatching the solve), and a `Store` does not exist outside a
        # real HA install at all.
        #
        # It is also NOT a missing port. The standalone/cron QUALITY
        # writer already assembles this decomposition inline -- see
        # `nimbus_solver_quality_writer.py` around its own
        # `compute_forecast_regret()` call -- from its own on-disk
        # `load_forecast_snapshot()`. #937's whole point is that the
        # native path was the side WITHOUT it, which is the asymmetry
        # this stage closes. The copy compared against here is the
        # FORECAST writer, which carries no quality machinery whatsoever.
        "_day_ahead_forecast_regret_attributes",
        "compute_efficiency_backtest_report",
        "compute_nimbus_only_soc_counterfactual",
        "publish_daily_quality_report",
        # nimbus issue #1120 part 3: the write-back half of the quality
        # scorer, driven by the nimbus_load.rescore_history service.
        # Native-only for the same reason every other quality function
        # above is -- the docs/cron copy compared against here is the
        # FORECAST writer, which carries no quality machinery at all
        # (no _compute_report_for_window, no _carry_forward_quality_
        # history, nothing this builds on). Quality has its own separate
        # cron script. Deliberately NOT claimed to be native-only in
        # principle: the function body is portable, and if the quality
        # cron script ever becomes the comparison target this should be
        # re-examined rather than left sitting here.
        "rescore_quality_history",
        "publish_efficiency_backtest_report",
        "publish_nimbus_only_soc_counterfactual",
        "publish_weather_forecast_mirrors",
        # nimbus issue #481: the fetch half of publish_weather_forecast_
        # mirrors() (already listed above), factored out so the new
        # thermal ambient-covariate wiring in apply_commanded_state_
        # guard() (itself native-only, listed further below) could reuse
        # it -- same native-only reasoning, not a separate gap.
        "_fetch_weather_hourly_forecast",
        "resolve_real_entity_id",
        "_load_token",
        # nimbus issue #486: reads real ConfigSubentries
        # (_NATIVE_HASS.config_entries.async_entries(...).subentries),
        # a concept that only exists inside a real running HA instance --
        # no standalone/cron equivalent config surface exists for
        # controllable-load subentries, and Mark's own #486 spec doesn't
        # ask for one. build_controllable_loads() returns ([], [])
        # unconditionally in standalone mode already (_NATIVE_HASS is
        # None there), so there's nothing behavioural to port even in
        # principle.
        "build_controllable_loads",
        # Private helper called only by build_controllable_loads (already
        # listed above) -- same native-only reasoning, not a separate gap.
        "_resolve_hour_to_period_index",
        # nimbus issue #612: same native-only reasoning as
        # _resolve_hour_to_period_index just above -- both are called
        # only from build_controllable_loads()'s own deferrable-load
        # branch (reads real ConfigSubentries), which already has no
        # standalone/cron equivalent. _period_index_for_instant is
        # _resolve_hour_to_period_index's own factored-out index-search
        # core; _build_daily_adequacy_windows builds the repeating daily
        # windows a same-day-shaped deferrable load now gets.
        "_period_index_for_instant",
        "_build_daily_adequacy_windows",
        # nimbus issue #479: samples a Controllable Load's real power
        # sensor into custom_components/nimbus_load/load_run_state.py's
        # own Store -- reads homeassistant.helpers.storage.Store and
        # ConfigSubentries data, same no-standalone-equivalent reasoning
        # as build_controllable_loads() itself, which is this function's
        # only caller.
        "_sample_load_run_state",
        # nimbus issue #484: the relay-chatter guard -- reads real
        # ConfigSubentries and homeassistant.helpers.storage.Store, same
        # no-standalone-equivalent reasoning as build_controllable_loads()
        # and _sample_load_run_state() above. A real no-op in standalone
        # mode anyway, since sheddable_loads/adequacy_loads are always
        # empty lists there.
        "apply_commanded_state_guard",
        # nimbus issue #476/#534: the output-layer dispatch itself -- calls
        # hass.services.async_call(), a real Home Assistant API with no
        # standalone/cron equivalent, same no-standalone-equivalent
        # reasoning as apply_commanded_state_guard() just above, which is
        # this function's only caller. Only ever invoked from native mode,
        # and does nothing at all when it isn't (apply_commanded_state_
        # guard() itself no-ops with _NATIVE_HASS is None).
        "dispatch_commanded_state",
        # nimbus issue #875: resolves this load's own re-send interval
        # from its subentry data. Pure -- no HA dependency of its own --
        # but called ONLY from apply_commanded_state_guard() above, which
        # is native-only, so there is nothing in standalone/cron mode that
        # could call it: Controllable Loads have no existence there at all
        # (build_controllable_loads() returns ([], []) unconditionally).
        # Exactly the _parse_done_when() case below -- not a fresh gap.
        "_resolve_reaffirm_after_seconds",
        # nimbus issue #480: reads a real done_entity's live state off
        # _NATIVE_HASS.states -- only ever called from build_
        # controllable_loads() itself, which is native-only; Mark's own
        # #480 spec doesn't ask for a standalone/cron config path either.
        "_evaluate_done_condition",
        # Pure comparison-DSL parser called only by _evaluate_done_
        # condition (already listed above) -- same native-only
        # reasoning, not a separate gap, even though it has no HA
        # dependency of its own.
        "_parse_done_when",
        # nimbus issue #592: dual-mode-capable (has its own REST fallback
        # branch, same shape as fetch_entity_history_range()), but its
        # only real caller is apply_commanded_state_guard()'s own thermal-
        # forecast wiring -- already INTENTIONAL_NATIVE_ONLY above, since
        # Controllable Loads have no standalone/cron existence at all
        # (build_controllable_loads() returns ([], []) unconditionally
        # there). Same "only ever invoked from native mode, and does
        # nothing at all when it isn't" reasoning as dispatch_
        # commanded_state() above -- not a fresh gap to add to the real,
        # tracked porting debt in KNOWN_OPEN_DRIFT_INTEGRATION_ONLY below.
        "fetch_entity_attribute_history_range",
        # nimbus issue #563: reads real battery_participant ConfigSubentries
        # (_NATIVE_HASS.config_entries.async_entries(...).subentries), same
        # no-standalone-equivalent reasoning as build_controllable_loads()
        # above -- no standalone/cron config surface exists for battery
        # participant subentries, and #563's own spec doesn't ask for one.
        # Returns [] unconditionally in standalone mode already (_NATIVE_
        # HASS is None there), so there's nothing behavioural to port.
        "build_extra_batteries",
        # nimbus issue #768/#585: same reasoning as build_extra_
        # batteries() immediately above -- reads real battery_participant
        # ConfigSubentries (_NATIVE_HASS.config_entries.async_entries(...)
        # .subentries), no standalone/cron equivalent exists, and returns
        # [] unconditionally in standalone mode already (_NATIVE_HASS is
        # None there), so there is nothing behavioural to port.
        "_resolve_battery_participant_history",
        # nimbus issue #1161: which grid periods a participant's power
        # history had no trustworthy sample behind them. Called from
        # exactly one place -- _resolve_battery_participant_history()
        # directly above -- which returns [] unconditionally in
        # standalone mode, so this is never reached there and there is
        # nothing behavioural to port.
        "_stale_power_period_indices",
        # nimbus issue #1181: how well a power sensor's recorder history
        # covers the scored window (points, median/max gap, two coverage
        # fractions). Its only caller is _compute_report_for_window(),
        # already listed in this set for the same reason -- the
        # standalone/cron script computes no quality report at all, so
        # there is nothing for this to be missing FROM. It also calls
        # _stale_power_period_indices() directly above, for the one
        # fraction that reuses the participant path's own one-hour guard.
        # Not a gap.
        "_power_history_coverage",
        # nimbus issue #1109 (2026-09-18): a pure helper (list of
        # (BatteryConfig, ndarray, ndarray, float) in, same out -- no HA
        # imports at all) whose ONLY caller is
        # _resolve_battery_participant_history() immediately above, itself
        # already INTENTIONAL_NATIVE_ONLY. Same reasoning as
        # _drop_implausible_power_samples() and
        # fetch_entity_state_history_range() below, and verified the same
        # way rather than assumed: the standalone/cron forecast writer has
        # ZERO references to battery participants or shared chargers, so
        # there is no code path there for this to protect.
        # nimbus issue #485/#582 (2026-09-18): the same-day-in-progress
        # correction, extracted from four identical inline copies. A pure
        # function (datetimes and ints in, int out, no HA imports) whose
        # callers are build_controllable_loads() and
        # apply_commanded_state_guard() -- both controllable-load
        # machinery, which the standalone/cron copy does not have at all
        # (build_controllable_loads is itself already listed here).
        "_earliest_period_for_same_day_window",
        "_widen_shared_charger_cap_to_achieved",
        # nimbus issue #1111 (2026-09-18): the second field-parity fix in
        # the same pass, same reasoning as the helper immediately above --
        # a pure function whose only caller is
        # _resolve_battery_participant_history(), and the cron forecast
        # writer has no battery participants to have a departure deadline
        # for in the first place.
        "_participant_departure_deadline",
        # nimbus issue #768 (2026-09-13): a genuinely dual-mode function
        # (it has its own REST branch, same shape as fetch_entity_
        # history_range()) but its ONLY caller is _resolve_battery_
        # participant_history() immediately above, itself already
        # INTENTIONAL_NATIVE_ONLY -- same "only ever invoked from native
        # mode, and does nothing at all when it isn't" reasoning
        # fetch_entity_attribute_history_range() already established
        # for exactly this shape, not a fresh gap.
        "fetch_entity_state_history_range",
        # nimbus issue #843 (2026-09-14, option B of Mark Purcell's own
        # A/B/C steer): a pure helper (list of (datetime, float) in, same
        # out -- no HA imports at all) but, exactly like fetch_entity_
        # state_history_range() immediately above, its ONLY caller is
        # _resolve_battery_participant_history(), itself already
        # INTENTIONAL_NATIVE_ONLY. Standalone/cron mode has no battery
        # participant subentries to reconstruct in the first place, so
        # there is no code path there for this guard to protect -- same
        # "only ever invoked from native mode, and does nothing at all
        # when it isn't" reasoning, not a fresh gap.
        "_drop_implausible_power_samples",
        # nimbus issue #843 (2026-09-14, option A of the same steer): a
        # genuinely dual-mode fetch (native + REST branches, same shape
        # as fetch_entity_history_range()) but, exactly like fetch_entity_
        # state_history_range() above, its ONLY caller is _resolve_
        # battery_participant_history(), itself already INTENTIONAL_
        # NATIVE_ONLY. Porting it would add a second, more expensive
        # recorder path to the standalone copy that nothing there can
        # reach -- standalone/cron mode has no battery participant
        # subentries at all.
        "fetch_entity_power_history_kw",
        # nimbus issue #645: overlays a Controllable Load's live
        # number.nimbus_<load>_<key> entity values on top of its own
        # subentry.data, same no-standalone-equivalent reasoning as
        # build_controllable_loads() above -- reads real ConfigSubentries
        # (via the subentry passed in) and _NATIVE_HASS.states, neither of
        # which exist in standalone/cron mode. Returns `data` completely
        # unchanged in standalone mode already (_NATIVE_HASS is None
        # there), so there's nothing behavioural to port.
        "_resolve_controllable_load_tuning",
        # nimbus issue #768 (Mark Purcell, 2026-09-14: "MQTT heat pump
        # device has a power sensor, please use it, through auto
        # discovery"). Reads Home Assistant's own ENTITY REGISTRY to find
        # the single device_class=power sensor on the same physical
        # device as a Controllable Load's device_entity -- there is no
        # registry at all in standalone/cron mode, and Controllable Loads
        # have no standalone existence anyway (build_controllable_loads()
        # returns ([], []) there), so there is nothing behavioural to
        # port. Same reasoning as _resolve_controllable_load_tuning()
        # immediately above, which this sits directly alongside.
        "_discover_power_sensor_for_device",
        "resolve_controllable_load_power_sensor",
        # Pure slug helper called only by _resolve_controllable_load_
        # tuning (already listed above) -- same native-only reasoning as
        # _parse_done_when's own listing above, even though it has no HA
        # dependency of its own. Deliberately duplicated (not imported)
        # from sensor.py's/number.py's own _slug_for_entity_id, per this
        # project's established small-pure-helper duplication convention.
        "_slug_for_controllable_load_entity_id",
        # nimbus issue #483 item 2: a pure function with no HA dependency
        # of its own, but its only real caller is main()'s own tariff-
        # attribution wiring, called with plan.adequacy_loads -- which the
        # docs copy's own main() always builds as adequacy_loads=[] (see
        # its Controllable-Load construction above), same no-standalone-
        # equivalent reasoning as build_controllable_loads() itself. A
        # real no-op ({}) in standalone/cron mode already; porting it
        # would add dead code with nothing to ever exercise it.
        "compute_tariff_attributed_cost",
        # nimbus issue #496 (Signals 7/7 of #489): same native-only
        # reasoning as compute_daily_quality_report() itself (already
        # listed above) -- compute_daily_flex_report() depends directly
        # on resample_history_mean() and fetch_entity_attribute_history_
        # range(), both already native-only in this same list, so this
        # is not a separate, freshly-introduced gap; porting it would
        # require porting those dependencies first, a genuinely larger,
        # separate piece of work out of this issue's own scope.
        "compute_daily_flex_report",
        "_compute_flex_report_for_window",
        "publish_daily_flex_report",
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
        # nimbus issue #467, staged item 3 -- solver_inputs/calendar_trips.py.
        # Resolves an HA calendar entity's upcoming events into adequacy
        # windows (trip distance -> kWh -> a window ending at departure).
        #
        # Deliberately NOT under INTENTIONAL_NATIVE_ONLY, for the same reason
        # the #452 helper above is not: this is portable in principle. HA's
        # `calendar.get_events` is a service with a response, and the cron copy
        # already reaches response-returning services over REST
        # (`ha_call_service_with_response`, used for weather.get_forecasts). So
        # calling it a permanent execution-context difference would be untrue.
        #
        # It is open drift rather than a port because the feature is not yet
        # wired on EITHER side -- this stage lands the resolution layer and its
        # tests, and #467's own item 4 asks for the end-to-end wiring to be
        # proven before the feature is called done (HAEO's abandoned #361 died
        # precisely from shipping calendar schema over wiring that did
        # nothing). Porting an unwired layer to a second copy first would
        # double that risk rather than reduce it.
        "parse_trip_distance_km",
        "trip_energy_kwh",
        "resolve_trip_windows",
        "trip_must_have_soc_kwh",
        "resolve_trip_deadline",
        # nimbus issue #1241: the power-sign convention detector and its
        # report-once helper.
        #
        # Integration-only for a structural reason, not an oversight: the
        # standalone/cron copy has NO battery-participant path at all
        # (`grep -c CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE` on it
        # returns 0), and that is where this check runs. Porting the detector
        # alone would put a function there with nothing to call it.
        #
        # `detect_power_sign_convention` itself is deliberately PURE -- zero
        # HA imports, loaded by path in its own tests so that is proven rather
        # than asserted -- specifically so it CAN be called from the
        # standalone path once that path grows participants. `_mean` is its
        # private helper and moves with it.
        "detect_power_sign_convention",
        "_mean",
        "_warn_sign_convention_once",
        # nimbus issue #1247: reports a participant whose scored history has
        # no away-gate. Same reason as the #1241 pair above -- the standalone
        # copy has no battery-participant path at all, so there is nothing
        # there for this to warn about.
        "_warn_participant_history_is_ungated_once",
        # nimbus issue #937 stage 1: the day-ahead forecast snapshot layer.
        #
        # Integration-only for now, and this one is a genuine open gap rather
        # than a structural difference: the standalone copy captures its
        # snapshot through a THIRD script (research/forecast_capture.py) into
        # a JSON file, which is why `load_forecast_snapshot` sits in the
        # quality writer's own drift list. This is the native equivalent of
        # that capture, and when the native path also consumes it the two
        # implementations should be reconciled rather than left to diverge.
        "build_snapshot",
        "day_key_for",
        "prune_snapshots",
        "resample_snapshot_to_grid",
        "_parse",
        # nimbus #467 item 4: reads an HA calendar entity via
        # calendar.get_events. Same open-drift reasoning as the four above, and
        # for the same reason it is NOT native-only: it goes through
        # ha_call_service_with_response(), which the cron copy already uses for
        # weather.get_forecasts, so the mechanism is genuinely portable. What
        # keeps it integration-only for now is the CONFIG surface -- the
        # participant subentry naming the calendar entity is a ConfigSubentry,
        # which is not exposed over this module's plain-REST seam, exactly as
        # build_extra_batteries() itself already documents.
        "fetch_calendar_trips",
        "_period_index_at",
        # nimbus issue #452, the forecast-vs-actuals half. The log-once
        # helper for it is integration-only because the check it reports
        # depends on two things the standalone/cron copy does not have:
        # the `aemo_crosscheck` module (a separate file, which porting
        # would mean copying wholesale or inlining), and the discovered
        # `aemo_30min_forecast_sensor` key, which is resolved by
        # sensor.py walking `hass.states` -- a module the cron script
        # has no equivalent of at all.
        #
        # Deliberately NOT filed under INTENTIONAL_NATIVE_ONLY: this is
        # portable in principle (the cron copy already reads
        # sensor.nimbus_solver_config over REST, so the discovered key
        # would reach it on any install that also runs the integration),
        # so calling it a permanent execution-context difference would
        # be untrue. Real, disclosed follow-up work -- same posture as
        # the #493 helpers below.
        "_warn_aemo_forecast_vs_actuals_once",
        "_hour_in_schedule_block",
        "_kw_scale_factor",
        # nimbus issue #493 (Signals 4/7 of #489, item 1): the new
        # envelope-limit-entity resolver and its log-once dedup helpers
        # all depend on _kw_scale_factor above, itself already an
        # untracked-for-porting gap -- porting these three without also
        # porting that dependency would leave the docs copy with a
        # broken reference, a genuinely separate, larger pre-existing
        # gap out of this issue's own scope. Real, disclosed follow-up
        # work, not silently dropped.
        "_note_envelope_limit_recovered",
        "_warn_envelope_limit_dropped_once",
        "resolve_envelope_limit_kw",
        "_load_solar_delivery_state",
        "_save_solar_delivery_state",
        "_log_active_household_specific_overrides_once",
        "_safe_fromisoformat",
        "blend_price_with_secondary_sources",
        # nimbus issue #1213 (Mark Purcell): the additive price-event
        # simulation. Genuinely PORTABLE -- it reads a sensor's own
        # {time, value} forecast and returns a per-period delta, with no
        # HA-native dependency, and both helpers it needs (ha_get,
        # parse_iso) already exist in the standalone copy. So this is
        # open drift to be reconciled, NOT a native-only concern.
        #
        # Listed beside blend_price_with_secondary_sources deliberately:
        # the standalone copy's price pipeline is already behind on that
        # function, and porting this one alone would layer a price-event
        # delta onto a series that never got the blend it is supposed to
        # sit on top of. The two want porting together, as one piece of
        # work on the standalone price path, rather than separately.
        "resolve_price_event_delta",
        "compute_cost_band",
        "compute_cost_breakdown",
        "fetch_entity_history_range",
        "periods_within_hours",
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

# A fourth category, in the same spirit as the three above: these exist
# as top-level defs in the integration ONLY because nimbus issue #735
# extracted them out of main(), while the docs/cron copy still has the
# equivalent logic inline in its own main(). They were never separate
# functions in either copy before the refactor, so there is nothing to
# "port" -- the standalone script has the same behaviour, just not
# factored out.
#
# Deliberately its own list rather than folded into INTENTIONAL_NATIVE_
# ONLY, for the reason this file's own docstring already gives for
# keeping three lists instead of one: these are NOT native-HA-only
# concerns (build_solar_arrays() would work perfectly well in standalone
# mode), and filing them as such would misrepresent why the difference
# exists. It is also not permanent in principle -- it would disappear if
# the standalone copy were ever refactored the same way -- but unlike
# KNOWN_OPEN_DRIFT it is not tracked WORK, because nothing is missing.
#
# Precedent: publish_plan/build_per_battery_forecast already carry
# exactly this reasoning inside INTENTIONAL_NATIVE_ONLY (from #363 step
# 2's own staged extraction). They are left where they are -- both are
# genuinely native-only as well as extracted, so their current listing
# is not wrong; this list is for extractions that have no second reason.
#
# Expected to GROW as #735 stages 2-4 land. That growth is fine and
# visible; what this file exists to prevent is a real missing FIX hiding
# among them, which it still catches -- a relocated function stays in
# the integration's union, so only genuinely new top-level functions
# ever land here.
INTENTIONAL_EXTRACTED_FROM_MAIN = frozenset(
    {
        # #735 stage 6 -- _publish_side_reports(), the six non-essential
        # side-publishes main() makes after the real solve (weather
        # mirrors, daily quality report, daily flex report,
        # counterfactual SoC, efficiency backtest, solar delivery ratio).
        # Still inline in the docs copy's own main(), so nothing is
        # missing from either side.
        #
        # Measured at STATEMENT level before moving -- 5 inputs, 1
        # output, 47 lines -- which is the cleanest seam taken so far
        # (solar had 3 outputs; load 4 in / 12 out; the SoC envelope
        # 5 in / 4 out). Found by scanning every consecutive-statement
        # window in main() rather than by reading for one, after stage
        # 4's own region measured as having no clean seam at all.
        #
        # That last clause is now out of date, and is kept rather than
        # rewritten because the sequence is the point: the same scan that
        # found stage 6 also reversed the "no clean seam" verdict on
        # stage 4, which has since landed as build_price_arrays below.
        # That region had been read for a seam twice and measured once
        # before scanning found it was a single `if` statement.
        "_publish_side_reports",
        # #735 stage 1 -- solver_inputs/solar.py. The three fetchers were
        # nested closures inside main() before the move (and still are in
        # the docs copy); build_solar_arrays() is the extracted block
        # itself; _solver_writer() is the deferred-import accessor the
        # split requires (see solver_inputs/__init__.py for why it is
        # deferred).
        "build_solar_arrays",
        "_fetch_solar_source_safe",
        "_fetch_open_meteo_solar_raw",
        "_fetch_solcast_solar_raw",
        "_solver_writer",
        # #735 stage 2 -- solver_inputs/load.py. build_load_arrays() is
        # the extracted block; the docs copy still has the identical
        # logic inline in its own main(), so nothing is missing from
        # either side.
        #
        # LoadArrays is a dataclass, not a def, so it does not appear
        # here -- worth noting because the twelve-output seam is exactly
        # why stage 2 needed one where stage 1 returned a plain tuple.
        "build_load_arrays",
        # #735 stage 5 -- solver_inputs/battery_soc.py.
        # resolve_soc_envelope() is the extracted block; verified
        # directly rather than assumed -- the docs copy still computes
        # max_soc_kwh_val inline in its own main() and still logs the
        # same floor/ceiling excursion. It does so without a warn-once
        # flag, which is correct there and not drift: the cron script
        # is one process per run, so "log this once per excursion" has
        # nothing to dedup against.
        #
        # SocEnvelope is a dataclass, not a def, so it does not appear
        # here -- same as LoadArrays above.
        "resolve_soc_envelope",
        # #735 / PR #888 -- solver_publish.py. Registered here by nimbus
        # issue #952 (Mark Purcell): this module was invisible to the
        # guard entirely until its path joined _EXTRACTED_PACKAGE_GLOBS,
        # so the annotation below never had to be written.
        #
        # Verified rather than assumed, the same way resolve_soc_envelope
        # above was: the docs copy still publishes this sensor inline in
        # its own main() -- a direct ha_post_state(
        # "sensor.nimbus_household_load_total_forecast", ...) call, with
        # the same #100 state-vs-forecast[0] fix and the same
        # failed_load_entities list. So the logic is present in both
        # copies; only the integration has given it a name.
        "publish_household_load_total_forecast",
        # #735 stage 4 -- solver_inputs/prices.py. build_price_arrays()
        # is the extracted block; the docs copy still has the identical
        # two-path price/P2P branch inline in its own main(), so nothing
        # is missing from either side. PriceArrays is a dataclass, not a
        # def, so it does not appear here.
        #
        # Measured at STATEMENT level before moving, and the measurement
        # is what made it safe: the region is ONE `if`/`else` statement
        # (5 in / 9 out, 272 lines), every returned name is assigned in
        # BOTH arms, and the move is a pure dedent whose re-indent is
        # byte-identical to what stood in main(). That last property is
        # the #873 guard -- hoisting a block that begins with `if` can
        # silently re-parent a following `elif`, and a pure indent
        # cannot.
        #
        # Mark Purcell independently re-measured the ledger after stage 6
        # and named this region as the largest remaining candidate, which
        # is what took it off the deferred list.
        "build_price_arrays",
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


def _integration_def_names() -> set[str]:
    """Every top-level def across the integration's solve surface -- the
    writer plus every module #735 has extracted out of it. See
    _integration_paths()'s own comment for why this is a union rather
    than solver_writer.py alone."""
    names: set[str] = set()
    for path in _integration_paths():
        names |= _top_level_def_names(path)
    return names


class TestDocsWriterFunctionSetDoesNotSilentlyDrift(unittest.TestCase):
    def setUp(self):
        self.integration_defs = _integration_def_names()
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
            - INTENTIONAL_EXTRACTED_FROM_MAIN
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
            f"INTENTIONAL_NATIVE_ONLY with a one-line reason (or, if it "
            f"is purely an artifact of #735 extracting it out of main() "
            f"and the docs copy still has the same logic inline, "
            f"INTENTIONAL_EXTRACTED_FROM_MAIN).",
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

    def test_the_integration_union_actually_includes_extracted_modules(self):
        """Guard on the mechanism itself, not the data: if
        _integration_paths() ever silently stops picking up an extracted
        module (a renamed directory, a changed glob), every test above
        would keep passing while quietly enforcing less. Anchored on a
        real function that lives OUTSIDE solver_writer.py."""
        writer_only = _top_level_def_names(_INTEGRATION_PATH)
        self.assertNotIn(
            "build_solar_arrays",
            writer_only,
            "build_solar_arrays should live in solver_inputs/solar.py, not "
            "solver_writer.py -- if it moved back, this test's premise is stale",
        )
        self.assertIn(
            "build_solar_arrays",
            self.integration_defs,
            "the integration def set no longer includes solver_inputs/ -- "
            "_integration_paths() has stopped finding the extracted modules, "
            "so #357's drift guarantee is now being enforced against only "
            "part of the real integration surface",
        )
        self.assertGreater(len(_integration_paths()), 1)

    def test_a_new_top_level_extraction_cannot_go_unregistered(self):
        """The durable half of nimbus issue #952 (Mark Purcell).

        Registering `solver_publish.py` fixes the one module that had
        gone unnoticed. It does nothing to stop the NEXT one: #735's own
        list says stages 2-4 are still to land, and any of them may put a
        module directly under `nimbus_load/` rather than inside
        `solver_inputs/`. That module would be invisible here for exactly
        the same reason, and every test in this file would keep passing
        while enforcing less.

        So: every top-level `solver_*.py` must be either part of the
        solve surface this guard reads, or named below as deliberately
        outside it. Adding a module and neither registering nor
        acknowledging it fails here rather than silently narrowing
        #357's guarantee.
        """
        # Deliberately outside the solve surface. `solver_runtime.py` is
        # HA scheduling machinery -- the tick loop, the overlap guard
        # (#757/#945) -- with no standalone counterpart at all: the cron
        # script IS the schedule, one process per run. Folding its defs
        # into the union would let a genuinely docs-only entry look
        # present in the integration, which is the opposite of what this
        # file is for.
        deliberately_outside = {"solver_runtime.py"}

        registered = {os.path.basename(p) for p in _integration_paths()}
        found = {
            os.path.basename(p)
            for p in glob.glob(os.path.join(_NIMBUS_DIR, "solver_*.py"))
        }

        unaccounted = found - registered - deliberately_outside
        self.assertEqual(
            unaccounted,
            set(),
            f"{sorted(unaccounted)} sit directly under nimbus_load/ but are "
            f"neither read by _integration_paths() nor listed as deliberately "
            f"outside the solve surface. This is #952's exact blind spot: a "
            f"fix applied only inside such a module and never ported to "
            f"docs/real-world-integration/files/nimbus_solver_forecast_writer.py "
            f"would pass this entire suite silently. Either add its path to "
            f"_EXTRACTED_PACKAGE_GLOBS (and annotate whatever it newly "
            f"surfaces), or add it to this test's own deliberately_outside "
            f"set with a one-line reason.",
        )

    def test_extracted_list_does_not_go_stale(self):
        """Same discipline the KNOWN_OPEN_DRIFT lists already get: an
        entry that no longer exists anywhere in the integration is a
        stale slot a different function could silently reuse."""
        for name in INTENTIONAL_EXTRACTED_FROM_MAIN:
            with self.subTest(name=name):
                self.assertIn(
                    name,
                    self.integration_defs,
                    f"{name!r} is listed in INTENTIONAL_EXTRACTED_FROM_MAIN "
                    f"but no longer exists in the integration -- remove it "
                    f"from the list (renamed, deleted, or inlined again).",
                )

    def test_the_four_lists_do_not_overlap(self):
        """Each difference should be explained by exactly one reason. An
        entry in two lists means one of them is wrong, and whichever is
        consulted first would silently mask the other."""
        named = {
            "INTENTIONAL_NATIVE_ONLY": INTENTIONAL_NATIVE_ONLY,
            "KNOWN_OPEN_DRIFT_INTEGRATION_ONLY": KNOWN_OPEN_DRIFT_INTEGRATION_ONLY,
            "INTENTIONAL_EXTRACTED_FROM_MAIN": INTENTIONAL_EXTRACTED_FROM_MAIN,
        }
        items = list(named.items())
        for i, (name_a, a) in enumerate(items):
            for name_b, b in items[i + 1 :]:
                with self.subTest(pair=f"{name_a}/{name_b}"):
                    self.assertEqual(
                        a & b,
                        set(),
                        f"{sorted(a & b)} appears in both {name_a} and "
                        f"{name_b} -- one of those reasons is wrong.",
                    )


if __name__ == "__main__":
    unittest.main()
