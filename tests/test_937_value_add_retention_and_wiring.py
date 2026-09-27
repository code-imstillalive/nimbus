"""nimbus issue #937: the day-ahead verdict has to SURVIVE, and then be acted on.

Two halves of one change, tested in one file because neither is useful alone.

## Half one: stage 3 published the number but nothing kept it

PR #1307 made the native path publish the day-ahead decomposition, which removed
the reason there were no days. It did not make them accumulate. The
decomposition is a headline attribute describing `latest_date` alone, so
tomorrow's 06:00 scoring overwrites today's and nothing keeps it -- and #937's
own top-priority action is literally "More days":

    days scored 14 / naive persistence beat Nimbus's forecast 11 /
    mean nimbus_value_add_dollars -$0.71/day / worst -$3.42

Those fourteen were readable only because the cron writer of the time put a
`forecast_regret` sub-dict into `day_entry`. A window like it cannot be rebuilt
from headline attributes at all: they hold one day. So one figure --
`nimbus_value_add_dollars`, under the one-character key `"f"` -- goes into the
history row, beside the five numbers it sits with.

**Measured, so the byte cost is on the record rather than assumed.** Read
read-only off the reference household's production install on 2026-09-27: the
quality report's whole attribute payload is 22,750 bytes, of which `history` is
1,548 across 11 rows (~140 bytes/row). `"f":-0.7123` costs ~13, so at the 60-day
cap this adds ~780 bytes -- against the eight separate figures a full
`forecast_regret` sub-dict would have cost per row, which is exactly what the
five-key allow-list was written to exclude.

## Half two: nothing connected the measurement to the decision

Fourteen measured days said the forecaster was costing money and the forecaster
kept being used. `select.nimbus_solver_load_forecast_source_policy` is the
connection, default `off`.

`TestTheDefaultPathIsUntouched` is the class that matters most here. The
reference household runs real battery dispatch through `build_load_arrays()`, so
"off changes nothing" is asserted against the actual wiring, not only against
the pure decision function -- including that the SAME LIST OBJECT comes back, and
that no quality-report read and no recorder read happen at all.

## Asserted structurally where importing is not worth the cost

`solver_writer.py` is ~19k lines and imports numpy plus the whole solver package
at module scope, so the row-writing assertions parse it -- the same choice
`test_937_publish_forecast_regret.py` and the docs-drift guard both make. The
`solver_inputs/load.py` wiring IS exercised behaviourally, because its seam
(`cfg` in, arrays out) is narrow enough to fake honestly.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_NIMBUS = _ROOT / "custom_components" / "nimbus_load"
_WRITER = _NIMBUS / "solver_writer.py"
_LOAD = _NIMBUS / "solver_inputs" / "load.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

_WRITER_SRC = _WRITER.read_text(encoding="utf-8")
_WRITER_TREE = ast.parse(_WRITER_SRC)

BNE = timezone(timedelta(hours=10))


def _load_selection_module():
    """The pure decision module, by path -- same loader and same reason as
    `test_937_forecast_source_selection.py`."""
    path = _NIMBUS / "solver" / "forecast_source_selection.py"
    spec = importlib.util.spec_from_file_location("forecast_source_selection", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["forecast_source_selection"] = module
    spec.loader.exec_module(module)
    return module


fss = _load_selection_module()


def _fn(name: str):
    for node in ast.walk(_WRITER_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in solver_writer.py")


def _src(name: str) -> str:
    return ast.get_source_segment(_WRITER_SRC, _fn(name))


def _const(name: str):
    for node in ast.walk(_WRITER_TREE):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in solver_writer.py")


# ---------------------------------------------------------------------------
# Half one: retention
# ---------------------------------------------------------------------------


class TestTheRowFieldIsOneCharacterAndDistinct(unittest.TestCase):
    """The byte budget is real: this rides in the payload measured at 22,750
    bytes against the recorder's 16 KB cap."""

    def test_the_key_is_a_single_character(self):
        self.assertEqual(len(_const("_QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD")), 1)

    def test_it_does_not_collide_with_an_existing_row_key(self):
        taken = {
            _const("_QUALITY_HISTORY_VERSION_FIELD"),
            _const("_QUALITY_HISTORY_RELIABILITY_FIELD"),
            _const("_QUALITY_HISTORY_PROVISIONAL_FIELD"),
            *_const("_QUALITY_HISTORY_FIELDS"),
        }
        self.assertNotIn(_const("_QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD"), taken)

    def test_only_the_value_add_is_retained_not_the_whole_decomposition(self):
        """The five-key allow-list exists to keep a year of rows inside one
        attribute payload, and it is right. Retaining the level/shape/solar
        attribution per row would be eight figures a day for a question that is
        about one day, not about a window."""
        for headline_only in (
            "forecast_regret_load_level_error_dollars",
            "forecast_regret_load_shape_error_dollars",
            "forecast_regret_solar_error_dollars",
        ):
            self.assertNotIn(
                f'history[day_key]["{headline_only}"]',
                _WRITER_SRC,
                f"{headline_only} must stay headline-only",
            )


class TestOnlyARealVerdictIsRecorded(unittest.TestCase):
    """A fabricated 0.0 would read as "the forecaster exactly matched
    persistence" -- the confident-wrong-number failure
    `_day_ahead_forecast_regret_attributes()`'s own six reason codes exist to
    prevent."""

    def setUp(self):
        # Executed rather than imported: solver_writer costs numpy + the whole
        # solver package at module scope, and this helper is self-contained.
        src = _src("_day_ahead_value_add_for_history")
        namespace: dict = {"math": __import__("math")}
        exec(compile(src, "<helper>", "exec"), namespace)  # noqa: S102
        self.helper = namespace["_day_ahead_value_add_for_history"]

    def test_a_real_verdict_is_returned(self):
        self.assertAlmostEqual(
            self.helper(
                {
                    "forecast_regret_reason": None,
                    "forecast_regret_nimbus_value_add_dollars": -0.71234567,
                }
            ),
            -0.7123,
        )

    def test_every_reason_string_suppresses_the_row_field(self):
        """All six of stage 3's reasons, plus the cron path's own. A day with no
        snapshot has no verdict, and absence is the honest record of that."""
        for reason in (
            "native_only",
            "no_forecast_snapshot_for_this_day",
            "snapshot_unusable_for_this_grid",
            "previous_day_history_unavailable",
            "no_persistence_baseline",
            "decomposition_solve_failed",
        ):
            self.assertIsNone(
                self.helper(
                    {
                        "forecast_regret_reason": reason,
                        # Deliberately populated: gating on the VALUE rather
                        # than the reason would accept this.
                        "forecast_regret_nimbus_value_add_dollars": -1.5,
                    }
                ),
                reason,
            )

    def test_a_missing_or_unusable_value_is_absent_not_zero(self):
        for entry in (
            {"forecast_regret_reason": None},
            {
                "forecast_regret_reason": None,
                "forecast_regret_nimbus_value_add_dollars": None,
            },
            {
                "forecast_regret_reason": None,
                "forecast_regret_nimbus_value_add_dollars": "sideways",
            },
            {
                "forecast_regret_reason": None,
                "forecast_regret_nimbus_value_add_dollars": float("nan"),
            },
            {
                "forecast_regret_reason": None,
                "forecast_regret_nimbus_value_add_dollars": float("inf"),
            },
            # A bool is an int in Python, and `True` is not a dollar figure.
            {
                "forecast_regret_reason": None,
                "forecast_regret_nimbus_value_add_dollars": True,
            },
        ):
            self.assertIsNone(self.helper(entry), entry)

    def test_an_entry_with_no_reason_key_at_all_is_absent(self):
        """A day_entry written by an older release carries neither key. It must
        not be read as a verdict of anything."""
        self.assertIsNone(self.helper({"epr": 0.9}))


class TestARowNeverLosesAVerdictItAlreadyHad(unittest.TestCase):
    """The row is the only place the day-ahead figure survives at all. A
    re-push on a cycle that cannot recompute it must carry it forward, the way
    `"r"` behaves and unlike `"v"`, which must NOT advance there (#1219)."""

    def test_the_writer_falls_back_to_the_prior_row(self):
        src = _WRITER_SRC
        field = "_QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD"
        self.assertIn(f"elif {field} in prior_row:", src)
        self.assertIn(f"history[day_key][{field}] = prior_row[", src)


class TestTheTrailingRecordIsReadSafely(unittest.TestCase):
    """`read_day_ahead_value_add_history()` decides what a live household
    dispatches on. Reading the wrong entity's rows is the failure that
    matters."""

    def test_it_resolves_the_real_entity_id(self):
        """On an instance where a `remote_homeassistant` mirror of ANOTHER
        Nimbus install has claimed the plain
        `sensor.nimbus_solver_quality_report`, the literal read returns the
        other household's record -- and this function's output chooses a load
        forecast for a real battery."""
        src = _src("read_day_ahead_value_add_history")
        self.assertIn("resolve_real_entity_id(QUALITY_ENTITY_ID)", src)

    def setUp(self):
        self.src = _src("read_day_ahead_value_add_history")

    def _run(self, attrs, raiser=None):
        import json
        import urllib.error

        namespace: dict = {
            "QUALITY_ENTITY_ID": "sensor.q",
            "resolve_real_entity_id": lambda e: e,
            "urllib": urllib,
            "json": json,
            "_QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD": "f",
        }

        def ha_get(_entity):
            if raiser is not None:
                raise raiser
            return {"attributes": attrs}

        namespace["ha_get"] = ha_get
        exec(compile(self.src, "<reader>", "exec"), namespace)  # noqa: S102
        return namespace["read_day_ahead_value_add_history"]()

    def test_it_reads_the_field_out_of_every_row_that_has_one(self):
        out = self._run(
            {
                "history": {
                    "2026-09-11": {"epr": 0.9, "f": -0.5224},
                    "2026-09-12": {"epr": 0.9, "f": 0.32},
                    # No `f` -- a day scored with no snapshot. Absent, not 0.0.
                    "2026-09-13": {"epr": 0.9},
                }
            }
        )
        self.assertEqual(out, {"2026-09-11": -0.5224, "2026-09-12": 0.32})

    def test_a_missing_or_malformed_history_is_no_record(self):
        for attrs in ({}, {"history": None}, {"history": []}, {"history": "x"}):
            self.assertEqual(self._run(attrs), {})

    def test_a_row_of_the_wrong_shape_is_skipped_not_fatal(self):
        out = self._run(
            {
                "history": {
                    "2026-09-11": {"f": -0.5},
                    "2026-09-12": "not a row",
                    17: {"f": -9.9},
                    "2026-09-13": {"f": "sideways"},
                    "2026-09-14": {"f": True},
                }
            }
        )
        self.assertEqual(out, {"2026-09-11": -0.5})

    def test_an_unreachable_ha_is_no_record_not_a_crash(self):
        import urllib.error

        for exc in (
            urllib.error.URLError("down"),
            TimeoutError(),
            OSError("refused"),
        ):
            self.assertEqual(self._run({}, raiser=exc), {})


# ---------------------------------------------------------------------------
# Half two: the wiring
# ---------------------------------------------------------------------------


class _FakeSolverWriter:
    """Stands in for the deferred `solver_writer` module.

    Counts the quality-report read, because "the default path does not read it
    at all" is a property worth measuring rather than inferring from a weight.
    """

    def __init__(self, record: dict | None = None):
        self.record = record or {}
        self.reads = 0
        # The real module exposes `_NATIVE_HASS`, and the code under test reads
        # it to tell native mode from standalone/REST. `None` is the
        # standalone value, which is the mode these tests exercise -- without
        # this the fake raises AttributeError rather than taking either path,
        # which is how CI caught it (#937).
        self._NATIVE_HASS = None

    def read_day_ahead_value_add_history(self) -> dict:
        self.reads += 1
        return dict(self.record)


class _FakeShared:
    """Stands in for `solver_shared`: a recorder read, a resampler, a scale
    factor and a logger. Counts recorder reads for the same reason."""

    def __init__(self, history=None, scale=1.0):
        self.history = history
        self.scale = scale
        self.fetches = 0
        self.warnings: list[str] = []
        self.infos: list[str] = []
        outer = self

        class _Log:
            def warning(self, msg, *args):
                outer.warnings.append(msg % args if args else msg)

            def info(self, msg, *args):
                outer.infos.append(msg % args if args else msg)

            def debug(self, msg, *args, **kw):
                pass

        self._LOGGER = _Log()

    def _kw_scale_factor(self, _entity):
        return self.scale

    def fetch_entity_history_range(self, _entity, _start, _end):
        self.fetches += 1
        return list(self.history or [])

    def resample_history_mean(self, pts, grid_times, period_hours):
        """Nearest-at-or-before over the supplied points.

        Deliberately simpler than the real time-weighted implementation: these
        tests are about which INSTANT each grid period looks up, which is the
        part `_seasonal_naive_load_kw()` owns. The averaging is
        `solver_shared`'s own and has its own tests.
        """
        out = []
        ordered = sorted(pts, key=lambda tv: tv[0])
        for gt in grid_times:
            value = 0.0
            for ts, v in ordered:
                if ts <= gt:
                    value = v
                else:
                    break
            out.append(value)
        return out


def _load_module(shared, writer):
    """Load `solver_inputs/load.py` by path with both seams faked.

    By path rather than as a package import so the fakes can be injected
    without patching a real import graph -- and so this test does not depend on
    `custom_components.nimbus_load.__init__` running.
    """
    selection_pkg_stub = type(sys)("solver")
    sys.modules.setdefault("solver", selection_pkg_stub)
    sys.modules["solver.forecast_source_selection"] = fss
    sys.modules["solver_shared"] = shared
    sys.modules["solver_writer"] = writer
    spec = importlib.util.spec_from_file_location("_load_inputs_937", _LOAD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_load_inputs_937"] = module
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=BNE)


def _grid(n=8, minutes=15):
    return [NOW + timedelta(minutes=minutes * i) for i in range(n)]


LOSING_RECORD = {f"2026-09-{day:02d}": -1.0 for day in range(5, 13)}


class TestTheDefaultPathIsUntouched(unittest.TestCase):
    """The reference household dispatches a real battery through this function.
    An install that has not opted in must behave exactly as it did before this
    code existed."""

    def setUp(self):
        self.shared = _FakeShared()
        self.writer = _FakeSolverWriter(LOSING_RECORD)
        self.mod = _load_module(self.shared, self.writer)

    def test_off_returns_the_same_list_object(self):
        """Identity, not equality. Equality would pass on a copy, and a copy
        means arithmetic ran on the live dispatch array."""
        load = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        out, decision, persistence = self.mod._resolve_load_forecast_source(
            {}, _grid(), 0.25, NOW, load
        )
        self.assertIs(out, load)
        self.assertIsNone(persistence)
        self.assertEqual(decision.persistence_weight, 0.0)

    def test_off_reads_neither_the_record_nor_the_recorder(self):
        self.mod._resolve_load_forecast_source({}, _grid(), 0.25, NOW, [1.0] * 8)
        self.assertEqual(self.writer.reads, 0)
        self.assertEqual(self.shared.fetches, 0)

    def test_an_absent_policy_key_is_off(self):
        """A config dict from an install whose bridge sensor predates the key --
        the #837 shape. It must read as `off`, never as a crash."""
        _out, decision, _p = self.mod._resolve_load_forecast_source(
            {"solver_load_forecast_source_policy": None}, _grid(), 0.25, NOW, [1.0] * 8
        )
        self.assertEqual(decision.reason, fss.REASON_POLICY_OFF)

    def test_a_decision_is_always_returned(self):
        """Never None, so the published attributes say which gate closed rather
        than going quiet -- the #1162 lesson."""
        _out, decision, _p = self.mod._resolve_load_forecast_source(
            {}, _grid(), 0.25, NOW, [1.0] * 8
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.source, fss.SOURCE_ML)


class TestMeasureReadsTheRecordButChangesNothing(unittest.TestCase):
    def setUp(self):
        self.shared = _FakeShared()
        self.writer = _FakeSolverWriter(LOSING_RECORD)
        self.mod = _load_module(self.shared, self.writer)

    def test_it_reads_the_record_and_leaves_the_array_alone(self):
        load = [1.0] * 8
        out, decision, persistence = self.mod._resolve_load_forecast_source(
            {"solver_load_forecast_source_policy": "measure"},
            _grid(),
            0.25,
            NOW,
            load,
        )
        self.assertEqual(self.writer.reads, 1)
        self.assertIs(out, load)
        self.assertIsNone(persistence)
        self.assertEqual(decision.reason, fss.REASON_PERSISTENCE_FAVOURED_MEASURE_ONLY)

    def test_it_never_touches_the_recorder(self):
        """No baseline is needed to recommend one, and a recorder read on every
        cycle of a 1-minute solver is not free."""
        self.mod._resolve_load_forecast_source(
            {"solver_load_forecast_source_policy": "measure"},
            _grid(),
            0.25,
            NOW,
            [1.0] * 8,
        )
        self.assertEqual(self.shared.fetches, 0)


class TestActingNeedsARealBaseline(unittest.TestCase):
    """A policy that asked for persistence and cannot have it must say so and
    keep the ML forecast -- never silently half-apply."""

    def _mod(self, history=None):
        self.shared = _FakeShared(history=history)
        self.writer = _FakeSolverWriter(LOSING_RECORD)
        return _load_module(self.shared, self.writer)

    def test_no_whole_house_sensor_means_no_persistence(self):
        mod = self._mod()
        load = [1.0] * 8
        out, decision, persistence = mod._resolve_load_forecast_source(
            {"solver_load_forecast_source_policy": "persistence"},
            _grid(),
            0.25,
            NOW,
            load,
        )
        self.assertIs(out, load)
        self.assertIsNone(persistence)
        self.assertEqual(decision.persistence_weight, 0.0)
        self.assertEqual(decision.reason, "persistence_baseline_unavailable")

    def test_the_refusal_keeps_the_evidence_so_it_is_diagnosable(self):
        """ "I set the policy and nothing changed" has to be answerable from the
        published attributes alone."""
        mod = self._mod()
        _out, decision, _p = mod._resolve_load_forecast_source(
            {"solver_load_forecast_source_policy": "persistence"},
            _grid(),
            0.25,
            NOW,
            [1.0] * 8,
        )
        self.assertEqual(decision.days_scored, len(LOSING_RECORD))
        self.assertEqual(decision.days_persistence_won, len(LOSING_RECORD))
        self.assertIsNotNone(decision.mean_value_add_dollars)

    def test_the_refusal_is_logged_loudly(self):
        mod = self._mod()
        mod._resolve_load_forecast_source(
            {"solver_load_forecast_source_policy": "persistence"},
            _grid(),
            0.25,
            NOW,
            [1.0] * 8,
        )
        self.assertTrue(any("#937" in w for w in self.shared.warnings))

    def test_an_empty_recorder_read_means_no_persistence(self):
        mod = self._mod(history=[])
        load = [1.0] * 8
        out, _d, persistence = mod._resolve_load_forecast_source(
            {
                "solver_load_forecast_source_policy": "persistence",
                "solver_whole_house_cross_check_sensor": "sensor.house",
            },
            _grid(),
            0.25,
            NOW,
            load,
        )
        self.assertIs(out, load)
        self.assertIsNone(persistence)

    def test_an_all_zero_baseline_is_refused(self):
        """An all-zero persistence baseline is not a baseline. The LP would plan
        a real battery against a household that draws nothing -- worse than the
        flattering-comparison problem the day-ahead scorer refuses it for."""
        history = [(NOW - timedelta(hours=h), 0.0) for h in range(24, 0, -1)]
        mod = self._mod(history=history)
        load = [1.0] * 8
        out, _d, persistence = mod._resolve_load_forecast_source(
            {
                "solver_load_forecast_source_policy": "persistence",
                "solver_whole_house_cross_check_sensor": "sensor.house",
            },
            _grid(),
            0.25,
            NOW,
            load,
        )
        self.assertIs(out, load)
        self.assertIsNone(persistence)


class TestThePersistenceBaselineLooksBackTwentyFourHours(unittest.TestCase):
    """Seasonal-naive at a 24 h period -- exactly the baseline
    `forecast_regret.py`'s J_persistence scenario is built from, so the policy
    acts on the same baseline the -$0.71/day was measured against."""

    def setUp(self):
        # A recognisable ramp over yesterday: hour h carries value h.
        self.history = [
            (NOW - timedelta(hours=24) + timedelta(hours=h), float(h))
            for h in range(25)
        ]
        self.shared = _FakeShared(history=self.history)
        self.writer = _FakeSolverWriter(LOSING_RECORD)
        self.mod = _load_module(self.shared, self.writer)

    def test_each_period_takes_the_value_from_the_same_time_yesterday(self):
        grid = [NOW + timedelta(hours=h) for h in range(6)]
        out = self.mod._seasonal_naive_load_kw(
            {"solver_whole_house_cross_check_sensor": "sensor.house"},
            grid,
            1.0,
            NOW,
        )
        # The fixture is a ramp: yesterday's hour h carries the value h, with
        # hour 0 sitting exactly 24 h before `now`.
        #
        # grid[0] == now    -> a 24 h lag lands on hour 0  == 0.0
        # grid[1] == now+1h -> hour 1 == 1.0
        # grid[5] == now+5h -> hour 5 == 5.0
        #
        # grid[0] mapping to a 24 h lag rather than to 0 is the point of the
        # `max(1, ...)`: a zero lag would hand the LP the CURRENT reading as its
        # own persistence baseline, which is not a forecast of anything.
        self.assertEqual(out[0], 0.0)
        self.assertEqual(out[1], 1.0)
        self.assertEqual(out[5], 5.0)

    def test_the_second_day_of_a_horizon_wraps_to_a_forty_eight_hour_lag(self):
        """One 24 h read serves a 48 h horizon: a grid time past now+24h looks
        back two days, landing in the same window. Without this, the tail of the
        plan would be whatever the resampler's flat-hold produced."""
        grid = [NOW + timedelta(hours=h) for h in (25, 30)]
        out = self.mod._seasonal_naive_load_kw(
            {"solver_whole_house_cross_check_sensor": "sensor.house"},
            grid,
            1.0,
            NOW,
        )
        self.assertEqual(out[0], 1.0)
        self.assertEqual(out[1], 6.0)

    def test_it_reads_the_recorder_once_per_hour_not_once_per_cycle(self):
        """A 1-minute solve cadence must not become sixty 24-hour recorder reads
        an hour."""
        cfg = {"solver_whole_house_cross_check_sensor": "sensor.house"}
        for minute in range(10):
            self.mod._seasonal_naive_load_kw(
                cfg, _grid(), 0.25, NOW + timedelta(minutes=minute)
            )
        self.assertEqual(self.shared.fetches, 1)
        # A new hour is a new read.
        self.mod._seasonal_naive_load_kw(cfg, _grid(), 0.25, NOW + timedelta(hours=1))
        self.assertEqual(self.shared.fetches, 2)

    def test_the_sensor_scale_factor_is_applied(self):
        """A whole-house meter reporting watts must not be read as kilowatts --
        a 1000x load array would dominate every dispatch decision."""
        self.shared.scale = 0.001
        self.mod._PERSISTENCE_HISTORY_CACHE.clear()
        out = self.mod._seasonal_naive_load_kw(
            {"solver_whole_house_cross_check_sensor": "sensor.house"},
            [NOW + timedelta(hours=1)],
            1.0,
            NOW,
        )
        self.assertAlmostEqual(out[0], 0.001)

    def test_a_negative_meter_reading_never_becomes_a_negative_demand(self):
        """The noisy-Modbus class this project already fixed once for the live
        P2P automation: a momentary negative whole-house read must not become a
        negative load in a plan."""
        self.mod._PERSISTENCE_HISTORY_CACHE.clear()
        self.shared.history = [
            (NOW - timedelta(hours=24) + timedelta(hours=h), -5.0 if h == 3 else 2.0)
            for h in range(25)
        ]
        out = self.mod._seasonal_naive_load_kw(
            {"solver_whole_house_cross_check_sensor": "sensor.house"},
            [NOW + timedelta(hours=h) for h in range(6)],
            1.0,
            NOW,
        )
        self.assertTrue(all(v >= 0.0 for v in out), out)

    def test_the_cache_does_not_grow_without_bound(self):
        cfg = {"solver_whole_house_cross_check_sensor": "sensor.house"}
        self.mod._PERSISTENCE_HISTORY_CACHE.clear()
        for hour in range(24):
            self.mod._seasonal_naive_load_kw(
                cfg, _grid(), 0.25, NOW + timedelta(hours=hour)
            )
        self.assertLessEqual(
            len(self.mod._PERSISTENCE_HISTORY_CACHE),
            self.mod._PERSISTENCE_CACHE_MAX_ENTRIES,
        )


class TestAFullPersistencePolicyActuallyReplacesTheArray(unittest.TestCase):
    """The point of the whole change. #937 item 4: "the honest product answer
    might be to use it"."""

    def setUp(self):
        self.history = [
            (NOW - timedelta(hours=24) + timedelta(hours=h), 3.0) for h in range(25)
        ]
        self.shared = _FakeShared(history=self.history)
        self.writer = _FakeSolverWriter(LOSING_RECORD)
        self.mod = _load_module(self.shared, self.writer)
        self.mod._PERSISTENCE_HISTORY_CACHE.clear()
        self.cfg = {
            "solver_load_forecast_source_policy": "persistence",
            "solver_whole_house_cross_check_sensor": "sensor.house",
        }

    def test_the_lp_gets_persistence(self):
        grid = [NOW + timedelta(hours=h) for h in range(4)]
        out, decision, persistence = self.mod._resolve_load_forecast_source(
            self.cfg, grid, 1.0, NOW, [9.0, 9.0, 9.0, 9.0]
        )
        self.assertEqual(decision.source, fss.SOURCE_PERSISTENCE)
        self.assertEqual(out, persistence)
        self.assertTrue(all(v == 3.0 for v in out), out)

    def test_a_blend_lands_between_the_two(self):
        grid = [NOW + timedelta(hours=h) for h in range(4)]
        self.writer.record = {
            # 6 of 8 days lost -> weight 0.75.
            **{f"2026-09-{day:02d}": -1.0 for day in range(5, 11)},
            "2026-09-11": +0.5,
            "2026-09-12": +0.5,
        }
        out, decision, _p = self.mod._resolve_load_forecast_source(
            {**self.cfg, "solver_load_forecast_source_policy": "blend"},
            grid,
            1.0,
            NOW,
            [11.0, 11.0, 11.0, 11.0],
        )
        self.assertAlmostEqual(decision.persistence_weight, 0.75)
        # 0.25 * 11 + 0.75 * 3 == 5.0
        for value in out:
            self.assertAlmostEqual(value, 5.0)

    def test_a_grid_length_mismatch_refuses_rather_than_blending(self):
        """`blend_load_forecast()` raises on a length mismatch; this seam must
        catch that BEFORE it reaches the LP, not propagate it into a failed
        solve cycle."""
        grid = [NOW + timedelta(hours=h) for h in range(4)]
        out, decision, persistence = self.mod._resolve_load_forecast_source(
            self.cfg, grid, 1.0, NOW, [9.0, 9.0]
        )
        self.assertIs(out, out)
        self.assertEqual(len(out), 2)
        self.assertIsNone(persistence)
        self.assertEqual(decision.reason, "persistence_baseline_unavailable")


class TestTheDecisionIsPublished(unittest.TestCase):
    """`load_forecast_source_used` already says which SENSORS the forecast came
    from. This says which FORECAST the LP consumed -- a different question, and
    the two together are the whole provenance of the load array."""

    def setUp(self):
        src = _src("_load_forecast_source_attributes")
        keys = _const("_LOAD_FORECAST_SOURCE_KEYS")
        namespace: dict = {"_LOAD_FORECAST_SOURCE_KEYS": keys}
        exec(compile(src, "<publish>", "exec"), namespace)  # noqa: S102
        self.fn = namespace["_load_forecast_source_attributes"]
        self.keys = keys

    def test_none_publishes_every_key_as_none(self):
        """#589: a consumer must never see a key appear and vanish between
        cycles. Omitting the keys would also make "this install cannot tell me"
        indistinguishable from "this install chose ml"."""
        out = self.fn(None)
        self.assertEqual(set(out), set(self.keys))
        self.assertTrue(all(v is None for v in out.values()))

    def test_a_real_decision_publishes_scalars(self):
        decision = fss.select_forecast_source(
            value_add_by_day=LOSING_RECORD,
            today=datetime(2026, 9, 13, tzinfo=BNE).date(),
            policy=fss.POLICY_BLEND,
        )
        out = self.fn(decision)
        self.assertEqual(set(out), set(self.keys))
        for key, value in out.items():
            self.assertIsInstance(value, (str, int, float), key)
        self.assertEqual(out["load_forecast_source_policy"], "blend")
        self.assertEqual(out["load_forecast_persistence_weight"], 1.0)

    def test_the_weight_is_rounded(self):
        decision = fss.ForecastSourceDecision(
            source="blend",
            persistence_weight=11 / 14,
            policy="blend",
            reason="x",
            days_scored=14,
            days_persistence_won=11,
            mean_value_add_dollars=-0.712345678,
        )
        out = self.fn(decision)
        self.assertEqual(out["load_forecast_persistence_weight"], 0.7857)
        self.assertEqual(out["load_forecast_source_mean_value_add_dollars"], -0.7123)

    def test_a_none_mean_survives_rounding(self):
        """`round(None, 4)` raises, and the mean is legitimately absent whenever
        the window held no scored day."""
        decision = fss.select_forecast_source(
            value_add_by_day={},
            today=datetime(2026, 9, 13, tzinfo=BNE).date(),
            policy="measure",
        )
        out = self.fn(decision)
        self.assertIsNone(out["load_forecast_source_mean_value_add_dollars"])

    def test_the_keys_are_published_on_the_plan_sensor(self):
        self.assertIn(
            "**_load_forecast_source_attributes(load_forecast_source_decision)",
            _WRITER_SRC,
        )


class TestTheBlendIsPlacedCorrectlyInTheLoadBlock(unittest.TestCase):
    """The position of the blend inside `build_load_arrays()` is load-bearing in
    both directions, and neither is visible from reading the blend itself."""

    def setUp(self):
        self.src = _LOAD.read_text(encoding="utf-8")

    def test_it_runs_after_the_cross_check_snapshot(self):
        """`summed_18_now_kw` is the forecast-vs-forecast cross-check diagnostic
        (#100/#429). Blending before it is snapshotted would silently turn it
        into a blend-vs-forecast comparison."""
        snapshot = self.src.index("summed_18_now_kw = load_kw[0]")
        blend = self.src.index("_resolve_load_forecast_source(")
        # The definition appears earlier in the file than either; measure the
        # CALL inside build_load_arrays, which is the last occurrence.
        blend = self.src.rindex("_resolve_load_forecast_source(")
        self.assertLess(snapshot, blend)

    def test_it_runs_before_the_live_period_zero_anchor(self):
        """Period 0 is a MEASURED meter reading, not a forecast. Persistence has
        nothing useful to say about the instant a meter is currently
        reporting."""
        blend = self.src.rindex("_resolve_load_forecast_source(")
        anchor = self.src.index("load_kw[0] = max(0.0, live_load_kw)")
        self.assertLess(blend, anchor)

    def test_the_uncertainty_band_moves_with_the_central_array(self):
        """The stochastic LP reads all three. Blending the centre alone would
        leave `lower <= central <= upper` intact only by luck."""
        self.assertIn("load_lower_kw = blend_load_forecast(", self.src)
        self.assertIn("load_upper_kw = blend_load_forecast(", self.src)


if __name__ == "__main__":
    unittest.main()
