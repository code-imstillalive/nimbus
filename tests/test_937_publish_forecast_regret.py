"""nimbus issue #937, stage 3: publish the day-ahead decomposition.

Stage 1 built the pure snapshot shape, stage 2 captured and persisted one per
local day. This is the stage that makes the number #937 is actually about
appear on the quality report -- for the first time on any HACS install.

## What #937 asked for, and what was missing

That issue measured naive persistence beating the ML load forecaster on 11 of
14 days (mean **-$0.71/day**) and named its own top-priority action: *"More
days."* There were none, because the decomposition is assembled only in the
standalone cron writer, so the series stopped the moment the reference
household moved to the native path.

`compute_forecast_regret()` was already native and already complete. The only
missing input was a record of what the forecast SAID the night before -- which
cannot be backfilled, because the forecast arrays have no recorder history at
all (`research/forecast_capture.py` established that by measurement).

## The contract every test here exists to defend

**A missing snapshot publishes None values and a reason, never zeros.**

A zero-filled forecast scores as one that confidently predicted darkness and
no load -- the worst forecast physically possible -- when the truth is that
there is nothing to judge. `TestAMissingSnapshotIsNeverAZeroScore` walks every
route to "unusable" and asserts a reason came back instead of a number.

## Why the attribution terms are published too

`forecast_regret.py`'s own docstring on `solar_error_dollars` answers #937's
central assumption directly:

    Published deliberately, because #937 assumes the load forecaster is the
    dominant term and nobody has checked. If this dominates, the issue is
    chasing the wrong forecaster.

Shipping the headline dollars alone would deliver the metric and leave the
question it exists to settle unanswered, so all three attribution terms are
published, and `test_the_attribution_terms_are_published` pins that.

## Why the cache exists at all

The scorer runs on a worker thread inside `sw.main()`, where a `Store` read
cannot be awaited. So the async wrapper makes sure the data is resident before
dispatching the executor job. `TestTheCacheIsPrimedOnTheEventLoop` asserts
that happens in the async function and NOT inside `_run_one_cycle()` -- the
same thread-boundary mistake stage 2's capture had to avoid.

That cache is also the fix for **#1295** (Mark Purcell, IV&V #1289): the
capture path used to construct a fresh `Store` and re-read the whole file on
every cycle, roughly 288 times a day. `TestTheStoreIsNotReReadEveryCycle`
measures that directly -- it counts real loads across many calls rather than
asserting the shape of the fix, because the shape is exactly what was
plausible-looking and wrong before.

## Asserted structurally, and why

solver_writer.py is ~19k lines and imports numpy plus the whole solver package
at module scope, so these tests parse it rather than importing it -- the same
choice the docs-drift guard makes. Where a behavioural assertion is possible
without that cost (the cache), it is behavioural.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_WRITER = _ROOT / "custom_components" / "nimbus_load" / "solver_writer.py"
_REGRET = _ROOT / "custom_components" / "nimbus_load" / "solver" / "forecast_regret.py"
_HELPER = "_day_ahead_forecast_regret_attributes"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(_ROOT))
from custom_components.nimbus_load import forecast_snapshot_store as fss

_WRITER_SRC = _WRITER.read_text(encoding="utf-8")
_WRITER_TREE = ast.parse(_WRITER_SRC)


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(_WRITER_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name + " not found in solver_writer.py")


def _src(name: str) -> str:
    return ast.get_source_segment(_WRITER_SRC, _fn(name))


def _keys() -> list[str]:
    for node in ast.walk(_WRITER_TREE):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "_FORECAST_REGRET_KEYS" for t in node.targets
        ):
            return [
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    raise AssertionError("_FORECAST_REGRET_KEYS not found")


def _call_site_kwargs() -> dict:
    for call in ast.walk(_fn("_compute_report_for_window")):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == _HELPER:
            return {k.arg: getattr(k.value, "id", None) for k in call.keywords}
    raise AssertionError("call site not found")


def _runtime_src(fn_name: str) -> str:
    from custom_components.nimbus_load import solver_runtime

    return inspect.getsource(getattr(solver_runtime, fn_name))


class TestTheKeySetIsStable(unittest.TestCase):
    """#589's lesson: a consumer must never see a key appear and vanish
    between windows. Every failure path returns the full key set."""

    def test_the_keys_are_all_prefixed_and_unique(self):
        keys = _keys()
        self.assertTrue(all(k.startswith("forecast_regret_") for k in keys))
        self.assertEqual(len(keys), len(set(keys)))

    def test_it_publishes_the_headline_numbers(self):
        keys = set(_keys())
        for expected in (
            "forecast_regret_j_star",
            "forecast_regret_j_forecast",
            "forecast_regret_j_persistence",
            "forecast_regret_dollars",
            "forecast_regret_persistence_dollars",
            "forecast_regret_nimbus_value_add_dollars",
        ):
            self.assertIn(expected, keys)

    def test_the_attribution_terms_are_published(self):
        """#937 assumes the load forecaster is the dominant error term.
        forecast_regret.py's own docstring says nobody has checked. Shipping
        the headline dollars alone would leave that unanswered by the very
        feature that claims to address it."""
        keys = set(_keys())
        for expected in (
            "forecast_regret_load_level_error_dollars",
            "forecast_regret_load_shape_error_dollars",
            "forecast_regret_solar_error_dollars",
        ):
            self.assertIn(expected, keys)

    def test_it_publishes_when_the_snapshot_was_captured(self):
        """A snapshot taken at 09:00 after a restart has already seen nine
        hours of the day it forecasts and will flatter itself. Without this
        key the number looks equally trustworthy either way."""
        self.assertIn("forecast_regret_snapshot_captured_at", _keys())

    def test_it_publishes_a_reason(self):
        self.assertIn("forecast_regret_reason", _keys())

    def test_every_published_number_maps_to_a_real_result_member(self):
        """The thing a mock could not catch: a rename inside the solver
        package would silently null a published attribute, and a nulled
        attribute reads downstream as "could not be computed" rather than as
        a bug. Checked against the dataclass fields and properties.

        The map is explicit rather than derived by stripping the prefix,
        because two published names deliberately do NOT match their member:
        `forecast_regret_dollars` is the member of the same name (so a
        derived rule would look for `dollars`), and
        `forecast_regret_persistence_dollars` renames
        `persistence_regret_dollars` to keep every published key under one
        prefix. A derived rule would have to be loosened until it stopped
        catching anything -- and a guard that cannot fail is worse than no
        guard, because it reads as coverage.
        """
        members: set[str] = set()
        for node in ast.walk(ast.parse(_REGRET.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef):
                members.add(node.name)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                members.add(node.target.id)

        published_to_member = {
            "forecast_regret_j_star": "j_star",
            "forecast_regret_j_forecast": "j_forecast",
            "forecast_regret_j_persistence": "j_persistence",
            "forecast_regret_dollars": "forecast_regret_dollars",
            "forecast_regret_persistence_dollars": "persistence_regret_dollars",
            "forecast_regret_nimbus_value_add_dollars": "nimbus_value_add_dollars",
            "forecast_regret_load_level_error_dollars": "load_level_error_dollars",
            "forecast_regret_load_shape_error_dollars": "load_shape_error_dollars",
            "forecast_regret_solar_error_dollars": "solar_error_dollars",
        }
        this_layers_own = {
            "forecast_regret_snapshot_captured_at",
            "forecast_regret_reason",
        }

        # Both directions. Without the second half, adding a key and
        # forgetting to map it would pass silently.
        for key in _keys():
            if key in this_layers_own:
                continue
            with self.subTest(key=key):
                self.assertIn(key, published_to_member, "key is unmapped")
                self.assertIn(published_to_member[key], members)
        for key in published_to_member:
            with self.subTest(mapped=key):
                self.assertIn(key, _keys(), "mapped key is no longer published")

    def test_each_mapped_member_is_actually_read_from_the_result(self):
        """The map above proves the members exist. This proves the function
        reads them -- a mapped-but-unread member would mean a published key
        whose value came from somewhere else entirely."""
        read: set[str] = set()
        for node in ast.walk(_fn(_HELPER)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "fr"
            ):
                read.add(node.attr)
        for member in (
            "j_star",
            "j_forecast",
            "j_persistence",
            "forecast_regret_dollars",
            "persistence_regret_dollars",
            "nimbus_value_add_dollars",
            "load_level_error_dollars",
            "load_shape_error_dollars",
            "solar_error_dollars",
        ):
            with self.subTest(member=member):
                self.assertIn(member, read)


class TestAMissingSnapshotIsNeverAZeroScore(unittest.TestCase):
    """The contract the whole feature rests on.

    Every early return must hand back all-None values plus a reason -- never
    a dict of numbers, and never a zero-filled forecast series.
    """

    def setUp(self):
        self.fn = _fn(_HELPER)
        self.src = _src(_HELPER)

    def test_every_early_return_carries_a_reason(self):
        dict_returns = [
            n
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)
        ]
        early = [r for r in dict_returns if len(r.value.keys) == 2]
        self.assertGreaterEqual(len(early), 6)
        for r in early:
            names = [k.value for k in r.value.keys if isinstance(k, ast.Constant)]
            self.assertIn("forecast_regret_reason", names)

    def test_every_early_return_spreads_the_blank_key_set(self):
        """Returning only a reason would make the other keys vanish for that
        window, which is exactly #589's failure."""
        early = [
            n
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Dict)
            and len(n.value.keys) == 2
        ]
        for r in early:
            self.assertIsNone(r.value.keys[0], "first entry is not a ** spread")

    def test_the_reasons_are_distinct_and_name_the_gate_that_closed(self):
        """One generic reason would be useless. #1176/#1188 are this repo's
        own record of a conflated reason pointing a household at a subsystem
        that was never reached."""
        reasons = set()
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "forecast_regret_reason"
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                    ):
                        reasons.add(v.value)
        for expected in (
            "native_only",
            "no_forecast_snapshot_for_this_day",
            "snapshot_unusable_for_this_grid",
            "previous_day_history_unavailable",
            "no_persistence_baseline",
            "decomposition_solve_failed",
        ):
            self.assertIn(expected, reasons)

    def test_it_never_constructs_a_zero_filled_forecast_series(self):
        """Stated directly: no route may substitute zeros for an absent
        forecast. Zeros would score as the worst forecast possible rather
        than as an absent one."""
        self.assertNotIn("[0.0] *", self.src)
        self.assertNotIn("zeros(", self.src)

    def test_a_missing_snapshot_returns_before_any_solve(self):
        """Cost, not just correctness: the no-snapshot path is the ordinary
        state of every install on its first day, and it must not pay for four
        LP solves to discover that."""
        self.assertLess(
            self.src.index("no_forecast_snapshot_for_this_day"),
            self.src.index("compute_forecast_regret"),
        )

    def test_an_all_zero_persistence_baseline_is_refused(self):
        """An all-zero baseline is not a baseline. It would make the forecast
        look arbitrarily good by comparison -- the exact direction of error
        this metric exists to detect."""
        self.assertIn("no_persistence_baseline", self.src)


class TestItCannotBreakTheQualityReport(unittest.TestCase):
    """A diagnostic that takes EPR down with it is worse than no
    diagnostic -- #366/#373's "degrade, never wedge"."""

    def setUp(self):
        self.src = _src(_HELPER)

    def test_the_solve_is_wrapped(self):
        self.assertGreaterEqual(
            len([n for n in ast.walk(_fn(_HELPER)) if isinstance(n, ast.Try)]), 2
        )

    def test_the_native_import_is_guarded(self):
        """The standalone/cron copy of this file has no Store and no cache to
        prime one. An unguarded import would make this function raise there,
        on a copy that already computes its own decomposition."""
        self.assertIn("except ImportError", self.src)
        self.assertIn("native_only", self.src)

    def test_none_attribution_terms_do_not_raise(self):
        """`round(None, 4)` raises. The attribution terms are legitimately
        absent whenever the level split is undefined (#1072's near-zero
        forecast), so the rounding helper has to tolerate None."""
        self.assertIn("_maybe", self.src)
        self.assertIn("None if value is None", self.src)


class TestItIsWiredIntoTheReport(unittest.TestCase):
    """#1267 shipped as a complete no-op this same week because it was wired
    to a step whose keys lived elsewhere. Wiring gets asserted, not assumed."""

    def test_the_helper_is_called_by_the_scorer(self):
        self.assertTrue(_call_site_kwargs())

    def test_its_output_is_spread_into_the_published_dict(self):
        """Computing the attributes and not publishing them is exactly the
        no-op shape #1267 shipped in."""
        spread = [
            n
            for n in ast.walk(_fn("_compute_report_for_window"))
            if isinstance(n, ast.Dict)
            and any(
                k is None
                and isinstance(v, ast.Name)
                and v.id == "forecast_regret_attrs"
                for k, v in zip(n.keys, n.values)
            )
        ]
        self.assertTrue(spread, "forecast_regret_attrs is never spread into a dict")

    def test_it_is_passed_the_unconstrained_oracle_grid(self):
        """All scenarios must plan against the same unconstrained grid. A
        pinned P2P export commitment would stop the battery responding to a
        forecast difference at all, masking the very thing being measured --
        #919's own note, which applies identically here."""
        kwargs = _call_site_kwargs()
        self.assertEqual(kwargs.get("grid"), "grid_oracle")
        self.assertEqual(kwargs.get("battery"), "battery_cfg")

    def test_it_reuses_the_scorers_own_resolved_sensors(self):
        """Re-deriving "which entity is the real load" from cfg would make
        two owners of one decision, and the two halves of one report would
        drift apart the first time that choice changed."""
        kwargs = _call_site_kwargs()
        self.assertEqual(kwargs.get("load_sensor"), "load_sensor")
        self.assertEqual(kwargs.get("solar_sensor"), "solar_sensor")
        self.assertEqual(kwargs.get("load_scale"), "load_scale")
        self.assertEqual(kwargs.get("solar_scale"), "solar_scale")
        self.assertNotIn("cfg", kwargs)

    def test_it_scores_the_same_real_series_the_oracle_does(self):
        """Scoring a differently-built "real" series would make j_star here
        incomparable with the EPR published beside it."""
        kwargs = _call_site_kwargs()
        self.assertEqual(kwargs.get("solar_real_kw"), "solar_kw")
        self.assertEqual(kwargs.get("load_real_kw"), "load_kw")

    def test_it_uses_the_cheap_history_path(self):
        """fetch_entity_power_history_kw()'s own docstring states it is
        deliberately scoped to ONE caller, because preserving per-row
        attributes makes a full-day recorder read materially heavier. A
        second caller would impose that cost on every install.

        Asserted on the CALL GRAPH, not the text. This function's docstring
        names that function in order to explain why it is not used, and a
        text search cannot tell an explanation from a call. An assertion
        that punished the explanation would push the reasoning out of the
        file -- the same trade `test_it_persists_nothing_itself()` in the
        stage 1 tests already had to get right, for the same reason.
        """
        called: set[str] = set()
        for node in ast.walk(_fn(_HELPER)):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name:
                    called.add(name)
        self.assertNotIn("fetch_entity_power_history_kw", called)
        self.assertIn("fetch_entity_history_range", called)
        self.assertIn("resample_history_mean", called)


class _CountingStore:
    """A Store double that COUNTS real loads and saves.

    Counting is the point. #1295 was a per-cycle disk read hiding behind a
    comment that claimed "the cost is one dict lookup per cycle" -- a
    structural assertion would have agreed with that comment. Only counting
    catches it.
    """

    def __init__(self, data=None, fail_on_save=False):
        self.data = dict(data or {})
        self.loads = 0
        self.saves = 0
        self.fail_on_save = fail_on_save

    async def async_load(self):
        self.loads += 1
        return dict(self.data)

    async def async_save(self, data):
        if self.fail_on_save:
            raise OSError("disk full")
        self.saves += 1
        self.data = dict(data)


class _StoreCacheHarness(unittest.TestCase):
    """Shared plumbing: one _CountingStore behind the real _store_for cache."""

    def setUp(self):
        fss.reset_snapshot_cache()
        self.store = _CountingStore()
        self._real = fss._store_for
        fss._store_for = self._fake_store_for

    def tearDown(self):
        fss._store_for = self._real
        fss.reset_snapshot_cache()

    def _fake_store_for(self, _hass, entry_id):
        return self.store

    def _load(self, entry="hub1"):
        return asyncio.run(fss._async_stored(None, entry))


class TestTheStoreIsNotReReadEveryCycle(_StoreCacheHarness):
    """nimbus issue #1295 (Mark Purcell, IV&V #1289)."""

    def test_repeated_reads_touch_the_store_once(self):
        self.store.data = {"2026-09-25": {"version": 1}}
        for _ in range(50):
            self._load()
        self.assertEqual(
            self.store.loads, 1, "the Store was re-read after the first load"
        )

    def test_ensure_loaded_is_cheap_after_the_first_call(self):
        """This runs on EVERY solve cycle from async_run_solve, so its steady
        -state cost is the whole question #1295 raised."""
        for _ in range(50):
            asyncio.run(fss.async_ensure_snapshots_loaded(None, "hub1"))
        self.assertEqual(self.store.loads, 1)

    def test_the_store_instance_itself_is_reused(self):
        """Mark's own point, and real on its own: a fresh Store per call is
        ~288 objects a day, and a fresh instance also carries no pending-write
        state, which defeats the delayed-save debounce."""
        fss._store_for = self._real  # exercise the real instance cache
        try:
            a = fss._store_for(object(), "hub1")
            b = fss._store_for(object(), "hub1")
            self.assertIs(a, b)
            self.assertIsNot(a, fss._store_for(object(), "hub2"))
        finally:
            fss._store_for = self._fake_store_for

    def test_a_different_entry_gets_its_own_cache_entry(self):
        """The Store key is per-entry. One shared cache slot would serve one
        install's forecast to another."""
        self._load("hub1")
        self._load("hub2")
        self.assertEqual(self.store.loads, 2)

    def test_a_write_updates_the_cache_instead_of_invalidating_it(self):
        """HA's own _StoreManager.async_invalidate() runs on every save, so
        after a write there is nothing left to re-read cheaply -- which is
        precisely why the data is held here rather than leaning on HA's
        cache."""
        self._load()
        asyncio.run(fss._async_save_stored(None, "hub1", {"2026-09-26": {"v": 1}}))
        self.assertEqual(self.store.saves, 1)
        self.assertEqual(self._load(), {"2026-09-26": {"v": 1}})
        self.assertEqual(self.store.loads, 1, "a save forced a fresh disk read")

    def test_a_failed_save_leaves_the_cache_matching_disk(self):
        """If the cache claimed a snapshot the file never got, every later
        cycle would skip a capture it still owed -- and the day would be lost
        silently, which is the one cost this feature exists to prevent."""
        self.store.fail_on_save = True
        self._load()
        with self.assertRaises(OSError):
            asyncio.run(fss._async_save_stored(None, "hub1", {"2026-09-26": {"v": 1}}))
        self.assertEqual(self._load(), {})


class TestTheSnapshotCache(_StoreCacheHarness):
    """The synchronous read the scorer performs on the worker thread."""

    def test_a_loaded_day_reads_back_synchronously(self):
        self.store.data = {"2026-09-25": {"version": 1, "points": [1]}}
        self._load()
        self.assertEqual(
            fss.get_cached_snapshot("2026-09-25"), {"version": 1, "points": [1]}
        )

    def test_an_unknown_day_is_None_not_an_empty_snapshot(self):
        """None means "skip the decomposition", never "assume zero forecast
        error" -- the same contract every other layer states."""
        self.store.data = {"2026-09-25": {"version": 1}}
        self._load()
        self.assertIsNone(fss.get_cached_snapshot("2026-01-01"))

    def test_nothing_loaded_yet_is_None(self):
        self.assertIsNone(fss.get_cached_snapshot("2026-09-25"))

    def test_two_entries_decline_rather_than_guess(self):
        """With two entries resident there is no honest way to tell whose
        forecast this is, and guessing would score one install's day against
        another install's forecast."""
        self.store.data = {"2026-09-25": {"version": 1}}
        self._load("hub1")
        self._load("hub2")
        self.assertIsNone(fss.get_cached_snapshot("2026-09-25"))

    def test_a_non_dict_stored_value_is_refused(self):
        self.store.data = {"2026-09-25": "corrupt"}
        self._load()
        self.assertIsNone(fss.get_cached_snapshot("2026-09-25"))

    def test_a_store_failure_is_swallowed(self):
        class _Boom:
            async def async_load(self):
                raise OSError("unreadable")

        fss._store_for = lambda _h, _e: _Boom()
        asyncio.run(fss.async_ensure_snapshots_loaded(None, "hub1"))
        self.assertIsNone(fss.get_cached_snapshot("2026-09-25"))

    def test_there_is_a_reset_hook(self):
        """Module state must not leak between config entries or between
        tests -- the same reason solver_runtime has reset_module_state()."""
        self.store.data = {"2026-09-25": {"version": 1}}
        self._load()
        fss.reset_snapshot_cache()
        self.assertIsNone(fss.get_cached_snapshot("2026-09-25"))

    def test_the_reset_hook_drops_the_store_instances_too(self):
        """A Store holds a `hass` reference, so keeping one past unload would
        pin a dead instance for the life of the process."""
        fss._store_for = self._real
        try:
            fss._store_for(object(), "hub1")
            self.assertTrue(fss._STORES)
            fss.reset_snapshot_cache()
            self.assertFalse(fss._STORES)
        finally:
            fss._store_for = self._fake_store_for


class TestTheCacheIsPrimedOnTheEventLoop(unittest.TestCase):
    def test_loading_happens_in_the_async_wrapper(self):
        self.assertIn("async_ensure_snapshots_loaded", _runtime_src("async_run_solve"))

    def test_loading_is_not_in_the_worker_thread_body(self):
        """A Store read must be awaited. _run_one_cycle() runs in an
        executor, which is the entire reason this cache exists."""
        self.assertNotIn(
            "async_ensure_snapshots_loaded", _runtime_src("_run_one_cycle")
        )

    def test_it_loads_before_dispatching_the_solve(self):
        """Loading after the executor job starts would race it, and the
        scorer would read an empty cache on the very cycle that needed it."""
        src = _runtime_src("async_run_solve")
        self.assertLess(
            src.index("async_ensure_snapshots_loaded"),
            src.index("async_add_executor_job"),
        )

    def test_unloading_clears_the_cache(self):
        """The Store is per-ENTRY. A remove-then-re-add would otherwise
        leave the previous entry's forecast in memory, where the scorer
        would read it as this entry's own -- and a stale snapshot scores as
        a real forecast rather than as a missing one, which is the one
        outcome worse than having no cache at all."""
        from custom_components.nimbus_load import solver_runtime

        fss._CACHE["hub1"] = {"2026-09-25": {"version": 1}}
        solver_runtime.reset_module_state()
        self.assertIsNone(fss.get_cached_snapshot("2026-09-25"))

    def test_every_retained_day_is_available_not_just_today(self):
        """The scorer scores a COMPLETED day, so yesterday's snapshot is the
        one it actually needs -- and a re-score reaches up to 30 days back.

        The cache holds the whole stored dict rather than a named subset, so
        there is no day-selection to get wrong here. An earlier draft primed
        an explicit [yesterday, today] list, which would have made every
        re-score older than yesterday report a missing snapshot that was
        sitting on disk the whole time.
        """
        store = _CountingStore({f"2026-09-{d:02d}": {"version": 1} for d in (1, 25)})
        real = fss._store_for
        fss._store_for = lambda _h, _e: store
        try:
            fss.reset_snapshot_cache()
            asyncio.run(fss.async_ensure_snapshots_loaded(None, "hub1"))
            self.assertIsNotNone(fss.get_cached_snapshot("2026-09-01"))
            self.assertIsNotNone(fss.get_cached_snapshot("2026-09-25"))
        finally:
            fss._store_for = real
            fss.reset_snapshot_cache()


if __name__ == "__main__":
    unittest.main(verbosity=2)
