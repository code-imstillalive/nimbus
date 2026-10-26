"""Gates on the source tree and its documents: every test file a comment
names exists, every entity a README names exists, the cron copy uses local
hours, relative imports resolve, sensor discovery is not mirrored, every
number field is read, diagnostic log levels hold, flattened entity names do
not collide, the config surface stays in budget, the vendored telemetry
schema matches, no flow adds a `section` its save path cannot flatten, the
price event's strings state their window rule, the solver's diagnostics steer
nothing, the cron copies keep the native writer's fixes, no external entity_id
is written into the code, the same-day window has one helper, the benchmark
doc names the real importers, the package never shadows stdlib `time`, and
the quality report's schema number tracks its fields (#1837 group D: kept,
merged, each shown to fail once on a planted violation)."""

from __future__ import annotations

import ast
import collections
import glob
import hashlib
import inspect
import json
import os
import pathlib
import re
import sys
import unittest
from datetime import UTC, datetime, time
from pathlib import Path

import _solver_path  # noqa: F401
from _writer_source import writer_source, writer_source_paths, writer_trees

from tests._ha_stubs import install_ha_stubs

install_ha_stubs()

import solver_writer

# --- test_readme_entity_references_exist ------------------------------------------------------
# Regression test for nimbus issue #364 (Mark Purcell, codebase review):
# `README.md` never mentioned `sensor.nimbus_health_report`, `switch.nimbus_
# solver_dispatch_dry_run`/`sensor.nimbus_solver_dispatch_dry_run`, `sensor.
# nimbus_solver_price_response_latency`, `sensor.nimbus_mirror_{temperature,
# humidity}_forecast`, or the `compute_quality_report` service, despite all of
# them being real, live entities/services -- a reviewer grepping the docs for
# any of these would conclude they don't exist.

_REPO_ROOT = Path(__file__).resolve().parent.parent


_README = _REPO_ROOT / "README.md"


_NIMBUS_LOAD = _REPO_ROOT / "custom_components" / "nimbus_load"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestReadmeEntityAndServiceReferencesAreReal(unittest.TestCase):
    def setUp(self):
        self.readme_text = _read(_README)
        self.sensor_py = _read(_NIMBUS_LOAD / "sensor.py")
        self.switch_py = _read(_NIMBUS_LOAD / "switch.py")
        self.services_py = _read(_NIMBUS_LOAD / "services.py")

    def _assert_readme_mentions(self, needle: str) -> None:
        self.assertIn(
            needle,
            self.readme_text,
            f"README.md no longer mentions {needle!r} -- if this entity/"
            "service was renamed or removed, update the README's own "
            "'Diagnostics, dry-run, and other hub sensors'/'Services' "
            "section rather than just deleting this assertion",
        )

    def test_health_report_sensor_documented_and_real(self):
        # test_health_report_sensor_documented_and_real
        self._assert_readme_mentions("sensor.nimbus_health_report")
        self.assertIn('"sensor.nimbus_health_report"', self.sensor_py)

        # test_dispatch_dry_run_documented_and_real
        self._assert_readme_mentions("sensor.nimbus_solver_dispatch_dry_run")
        self._assert_readme_mentions("switch.nimbus_solver_dispatch_dry_run")
        self.assertIn("nimbus_solver_dispatch_dry_run", self.sensor_py)
        self.assertIn("CONF_SOLVER_DISPATCH_DRY_RUN", self.switch_py)

        # test_price_response_latency_documented_and_real
        self._assert_readme_mentions("sensor.nimbus_solver_price_response_latency")
        self.assertIn('"sensor.nimbus_solver_price_response_latency"', self.sensor_py)

        # test_mirror_forecast_sensors_documented_and_real
        self._assert_readme_mentions("sensor.nimbus_mirror_temperature_forecast")
        self._assert_readme_mentions("sensor.nimbus_mirror_humidity_forecast")
        self.assertIn("nimbus_mirror_temperature_forecast", self.sensor_py)
        self.assertIn("nimbus_mirror_humidity_forecast", self.sensor_py)

        # test_compute_quality_report_service_documented_and_real
        self._assert_readme_mentions("nimbus_load.compute_quality_report")
        self.assertIn(
            'SERVICE_COMPUTE_QUALITY_REPORT = "compute_quality_report"',
            self.services_py,
        )

        # test_retrain_and_solve_now_services_documented_and_real
        self._assert_readme_mentions("nimbus_load.retrain")
        self._assert_readme_mentions("nimbus_load.solve_now")
        self.assertIn('SERVICE_RETRAIN = "retrain"', self.services_py)
        self.assertIn('SERVICE_SOLVE_NOW = "solve_now"', self.services_py)

        # test_every_domain_service_constant_is_mentioned_somewhere_in_readme
        # Broader net: catch a FUTURE new service that never gets added to
        # the README at all, not just the three named above.
        service_constants = re.findall(
            r'^SERVICE_\w+\s*=\s*"([a-z_]+)"', self.services_py, re.MULTILINE
        )
        self.assertTrue(
            service_constants, "no SERVICE_* constants found -- test setup issue"
        )
        for service_name in service_constants:
            with self.subTest(service=service_name):
                self.assertIn(
                    f"nimbus_load.{service_name}",
                    self.readme_text,
                    f"service '{service_name}' exists in services.py but "
                    "README.md never mentions it",
                )


# --- test_source_comment_test_references_exist ------------------------------------------------
# Every `test_*.py` filename a source comment points at must exist.

_REPO_ROOT_source_comment_test_refe = Path(__file__).resolve().parent.parent


_INTEGRATION = _REPO_ROOT_source_comment_test_refe / "custom_components" / "nimbus_load"


_TESTS = _REPO_ROOT_source_comment_test_refe / "tests"


# nimbus issue #955: the standalone/cron writer is real shipped source a
# household actually runs, and its own test-file references were never
# swept. Same guarantee, same failure shape, one more root.
_STANDALONE = (
    _REPO_ROOT_source_comment_test_refe / "docs" / "real-world-integration" / "files"
)


_SCANNED_ROOTS = (_INTEGRATION, _STANDALONE)


# A bare `test_foo.py`, or a `tests/test_foo.py` path form. Both appear
# in the tree today.
_REFERENCE = re.compile(r"\b(?:tests/)?(test_[A-Za-z0-9_]+\.py)\b")


def _existing_test_filenames() -> set[str]:
    return {p.name for p in _TESTS.rglob("test_*.py")}


def _references() -> dict[str, set[str]]:
    """{referenced filename: {source files naming it}} across the whole
    integration package, plus the standalone/cron writer (#955)."""
    found: dict[str, set[str]] = {}
    for root in _SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in _REFERENCE.finditer(text):
                rel = path.relative_to(_REPO_ROOT_source_comment_test_refe).as_posix()
                found.setdefault(match.group(1), set()).add(rel)
    return found


class TestSourceCommentsPointAtRealTestFiles(unittest.TestCase):
    def test_every_referenced_test_file_exists(self):
        # test_every_referenced_test_file_exists
        existing = _existing_test_filenames()
        references = _references()
        self.assertTrue(
            references,
            "sanity check: found no test-file references in the integration "
            "at all, so this test would pass against any source tree",
        )
        broken = {
            name: sorted(where)
            for name, where in sorted(references.items())
            if name not in existing
        }
        self.assertEqual(
            broken,
            {},
            "these source comments name a test file that does not exist. A "
            "name that resolves to nothing reads as 'this invariant is "
            "unguarded' -- point it at the file that really carries the "
            "guard (it usually still exists under a different name after a "
            "split or rename), or drop the reference if the guard is "
            f"genuinely gone: {broken}",
        )

        # test_the_standalone_writer_is_actually_being_swept: Guard on the mechanism, not the data (nimbus issue #955).
        self.assertTrue(
            _STANDALONE.is_dir(),
            f"{_STANDALONE} no longer exists -- the standalone/cron writer has "
            "moved, and this file is silently no longer sweeping it",
        )
        sources = {where for wheres in _references().values() for where in wheres}
        self.assertTrue(
            any(s.startswith("docs/real-world-integration/files/") for s in sources),
            "no test-file reference was found in the standalone/cron writer at "
            "all. Either every one was removed (fine -- delete this test with "
            "them), or the sweep has stopped reaching that directory.",
        )

        # test_the_two_known_corrections_stay_correct: The specific pair this file was written for, pinned by name so
        existing = _existing_test_filenames()
        for expected in (
            # The guard moved to tests/v2 with #1837.
            "test_controllable_load_sensors.py",
            "test_battery_config_validation.py",
        ):
            with self.subTest(expected):
                self.assertIn(expected, existing)


# --- test_local_hour_is_used_everywhere_in_the_cron_copy --------------------------------------
# IV&V finding (since-4812f93 pass, 2026-09-18): #1076's AST guard
# (`tests/test_local_hour_is_used_everywhere.py`) protects only
# `custom_components/nimbus_load/solver_writer.py` -- the standalone/cron
# deployment's own copy, `docs/real-world-integration/files/
# nimbus_solver_forecast_writer.py`, has no equivalent guard at all.

_SRC = (
    pathlib.Path(__file__).resolve().parent.parent
    / "docs"
    / "real-world-integration"
    / "files"
    / "nimbus_solver_forecast_writer.py"
)


# Same exemption list as the native guard's own test file -- neither
# name is a datetime field in this file either.
_NOT_A_CLOCK_READ = {"hours", "hourly_regret"}


def _is_local_call(node: ast.AST) -> bool:
    """Same recognizer as tests/test_local_hour_is_used_everywhere.py's
    own `_is_local_call()` -- `_local(x)` or `x.astimezone(...)`,
    walking through a `.time()`/`.date()` link."""
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "_local":
            return True
        if isinstance(fn, ast.Attribute):
            if fn.attr == "astimezone":
                return True
            if fn.attr in ("time", "date"):
                return _is_local_call(fn.value)
    return False


class TestTheCronCopyIsGuardedTheSameWay(unittest.TestCase):
    # FIXED 2026-09-18. All four sites this pin found now go through
    # `_local()`, so the `xfail(strict=True)` marker that stood here has
    # been removed -- with the fix in place it XPASSes, which is what the
    # strict marker is for.
    #
    # The one that mattered is `seconds_to_settlement_capture()`'s
    # `now.minute % 5`. The other three were convention risks -- every
    # `grid_times` element happens to be BRISBANE_TZ-aware because this
    # file's own `main()` builds `now` that way -- but that one is a real
    # latent bug for any NEM region without a whole-hour UTC offset,
    # because the function is documented as a pure function of `now`
    # precisely so a caller can hand it whatever it has. Adelaide is
    # UTC+9:30.
    #
    # This guard now runs permanently against the cron copy, so the
    # #1076 class cannot silently reappear in it independently of the
    # native guard -- which is the durable half of the finding.
    def test_no_bare_dot_hour_or_dot_minute_read_in_the_cron_copy(self):
        tree = ast.parse(_SRC.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr not in ("hour", "minute"):
                continue
            if node.attr in _NOT_A_CLOCK_READ:
                continue
            if _is_local_call(node.value):
                continue
            offenders.append(f"line {node.lineno}: .{node.attr} read without _local()")
        self.assertEqual(
            offenders,
            [],
            "the cron/standalone copy must be swept for bare .hour/.minute "
            "reads the same way the native integration already is -- a "
            "future edit to either copy can silently reintroduce exactly "
            "the #1076 bug in whichever copy has no guard.\n\nOffenders:\n  "
            + "\n  ".join(offenders),
        )


# --- test_relative_import_depth_resolves ------------------------------------------------------
# Every relative import in the integration must resolve to something that
# exists.

_NIMBUS_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
)


def _resolve(module_path: pathlib.Path, level: int, module: str | None) -> pathlib.Path:
    """The directory a relative import's `level` dots point at.

    `level=1` is the package containing this module, `level=2` its parent, and so
    on -- exactly Python's own rule.
    """
    base = module_path.parent
    for _ in range(level - 1):
        base = base.parent
    return base if module is None else base / module.replace(".", "/")


def _exists(target: pathlib.Path) -> bool:
    return (
        target.is_dir()
        and (target / "__init__.py").exists()
        or target.with_suffix(".py").exists()
    )


def _names_in_init(package_dir: pathlib.Path) -> frozenset[str]:
    """Top-level names a package's own `__init__.py` defines.

    `from . import X` is valid when X is a submodule OR a name defined in the
    package's `__init__.py` -- `number.py` and `switch.py` both do the latter with
    `_configure_price_watcher`, and `flows/signal_subentry.py` with
    `find_subentry_sharing_source_sensor`. Without this the check reports three
    false positives and would have to be weakened to stay green, which is how a
    guard stops guarding.
    """
    init = package_dir / "__init__.py"
    if not init.exists():
        return frozenset()
    names: set[str] = set()
    for node in ast.parse(init.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.AnnAssign):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return frozenset(names)


class TestRelativeImportDepthResolves(unittest.TestCase):
    def test_every_relative_import_points_at_something_real(self):
        # test_every_relative_import_points_at_something_real: A relative import whose target does not exist on disk is a runtime
        checked = 0
        broken: list[str] = []
        for path in sorted(_NIMBUS_DIR.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                base = _resolve(path, node.level, node.module)
                if node.module is None:
                    # `from . import a, b` -- each name is a submodule OR a name
                    # the target package's own __init__.py defines
                    init_names = _names_in_init(base)
                    for alias in node.names:
                        checked += 1
                        if (
                            not _exists(base / alias.name)
                            and alias.name not in init_names
                        ):
                            rel = (base / alias.name).relative_to(_NIMBUS_DIR.parent)
                            broken.append(
                                f"{path.relative_to(_NIMBUS_DIR)}:{node.lineno}  "
                                f"from {'.' * node.level} import {alias.name}"
                                f"  ->  {rel}"
                            )
                else:
                    checked += 1
                    # `from .pkg import NAME` -- resolve the MODULE only; the
                    # imported names may be attributes rather than submodules.
                    if not _exists(base):
                        broken.append(
                            f"{path.relative_to(_NIMBUS_DIR)}:{node.lineno}  "
                            f"from {'.' * node.level}{node.module} import ..."
                            f"  ->  {base.relative_to(_NIMBUS_DIR.parent)}"
                        )
        self.assertGreater(checked, 100, "non-vacuity: too few relative imports found")
        self.assertEqual(
            broken,
            [],
            "relative imports whose target does not exist. Under pytest these are "
            "masked by pyproject's `pythonpath` making the `except ImportError` "
            "absolute fallback succeed; under real Home Assistant they raise and "
            "kill the solve. This is nimbus #1302's own regression, shipped in "
            "v0.94.428 and found on devhub:\n  " + "\n  ".join(broken),
        )

        # test_the_check_would_have_caught_the_v0_94_428_regression: Non-vacuity, and a guard against the resolver silently accepting
        inner = _NIMBUS_DIR / "solver_inputs" / "controllable_loads.py"
        self.assertTrue(inner.exists(), "fixture module moved")

        # one dot from inside solver_inputs/ -> solver_inputs/done_condition.py
        one_dot = _resolve(inner, 1, None) / "done_condition"
        self.assertFalse(
            _exists(one_dot),
            "`from . import done_condition` must NOT resolve from inside "
            "solver_inputs/ -- if this passes, done_condition.py has been added "
            "there and this test's premise is stale",
        )

        # two dots -> nimbus_load/done_condition.py, which is where it really is
        two_dot = _resolve(inner, 2, None) / "done_condition"
        self.assertTrue(
            _exists(two_dot),
            "`from .. import done_condition` must resolve from inside solver_inputs/",
        )

        # test_controllable_loads_uses_the_right_depth_everywhere: The specific file that shipped broken, pinned by name.
        path = _NIMBUS_DIR / "solver_inputs" / "controllable_loads.py"
        siblings = {p.stem for p in path.parent.glob("*.py")} - {"__init__"}
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            names = (
                [a.name for a in node.names]
                if node.module is None
                else [node.module.split(".")[0]]
            )
            init_names = _names_in_init(path.parent)
            for name in names:
                if name not in siblings and name not in init_names:
                    offenders.append(f"line {node.lineno}: {name}")
        self.assertEqual(
            offenders,
            [],
            "single-dot imports in controllable_loads.py that are not "
            f"solver_inputs/ siblings: {offenders}",
        )


# --- test_sensor_discovery_is_not_mirrored ----------------------------------------------------
# The discovery rules must live in exactly one place (nimbus issue #954, Mark
# Purcell, 48-hour IV&V #950).

_REPO_ROOT_sensor_discovery_is_not_ = Path(__file__).resolve().parent.parent


_INTEGRATION_sensor_discovery_is_not_ = (
    _REPO_ROOT_sensor_discovery_is_not_ / "custom_components" / "nimbus_load"
)


_OWNER = "sensor_discovery.py"


# The literals that define the rules. A second implementation has to
# name the suffix it filters on, so these are what a mirror cannot hide.
_DEFINING_LITERALS = ("_geocoded_location", "_current_30min_forecast")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every string node that is a module/class/function
    docstring, so prose can be excluded from the sweep."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _modules_with_literal_in_code(literal: str) -> set[str]:
    hits: set[str] = set()
    for path in sorted(_INTEGRATION_sensor_discovery_is_not_.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or id(node) in docstrings:
                continue
            if isinstance(node.value, str) and literal in node.value:
                hits.add(path.name)
    return hits


class TestDiscoveryRulesAreNotMirrored(unittest.TestCase):
    def test_each_rule_is_defined_in_exactly_one_module(self):
        # test_each_rule_is_defined_in_exactly_one_module
        for literal in _DEFINING_LITERALS:
            with self.subTest(literal=literal):
                owners = _modules_with_literal_in_code(literal)
                self.assertEqual(
                    owners,
                    {_OWNER},
                    f"the {literal!r} discovery rule must be defined only in "
                    f"{_OWNER}, but executable code naming it was found in "
                    f"{sorted(owners)}. A second copy is how #954 happened: "
                    f"two implementations that agree today, one of which the "
                    f"tests exercise and the other of which ships. Call the "
                    f"function in {_OWNER} instead of re-filtering.",
                )

        # test_the_sweep_can_actually_see_code: The mechanism, not the data: if `_modules_with_literal_in_code`
        for literal in _DEFINING_LITERALS:
            with self.subTest(literal=literal):
                self.assertIn(
                    _OWNER,
                    _modules_with_literal_in_code(literal),
                    f"{_OWNER} no longer contains executable code naming "
                    f"{literal!r} -- either the rule moved (update _OWNER) or "
                    f"this sweep has stopped seeing code at all",
                )

        # test_prose_is_excluded_rather_than_the_files_being_lucky: `sensor.py` still describes both entity shapes in its
        sensor_py = (_INTEGRATION_sensor_discovery_is_not_ / "sensor.py").read_text(
            encoding="utf-8"
        )
        for literal in _DEFINING_LITERALS:
            with self.subTest(literal=literal):
                self.assertIn(
                    literal,
                    sensor_py,
                    "sensor.py no longer mentions this entity shape at all, so "
                    "this test is no longer proving the prose exclusion works",
                )


# --- test_solver_number_fields_are_actually_read ----------------------------------------------
# nimbus issue #1013 -- a dashboard dial can be fully wired into the config
# surface and still be read by nothing.

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"


# nimbus issue #1013. The single known-inert field WAS
# `solver_battery_soh_percent`; its entry was deleted when the fix
# landed, exactly as the exemption's own text instructed, and
# `resolve_effective_capacity_kwh()` now reads it on every solve path.
#
# The dict stays, empty, because it is the mechanism rather than the
# finding: a new inert field fails `test_no_solver_number_is_silently_
# inert` immediately, and the only sanctioned way past that is to add an
# entry here with an issue number and a reason. Deliberately not a
# blanket "ignore unknown fields".
KNOWN_INERT: dict[str, str] = {}


def _solver_number_fields() -> set[str]:
    """Every `solver_*` config key `number.py` builds an entity for."""
    nums = (_ROOT / "number.py").read_text(encoding="utf-8")
    const = (_ROOT / "const.py").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bCONF_[A-Z0-9_]+\b", nums))
    values = dict(
        re.findall(
            r"^(CONF_[A-Z0-9_]+)\s*:\s*Final\s*=\s*\"([^\"]+)\"", const, re.MULTILINE
        )
    )
    return {
        values[c] for c in referenced if c in values and values[c].startswith("solver_")
    }


class TestEverySolverNumberIsActuallyRead(unittest.TestCase):
    def setUp(self):
        self.fields = _solver_number_fields()
        # nimbus issue #1301 (Phase 2, spec 001, #1347): several of these
        # fields are read inside functions that moved to solver_shared.py
        # (import_fee_rate's NETWORK_FEE_BLOCK_KEYS, fetch_p2p_fixed_
        # export_kw's P2P_BLOCK_KEYS/solver_p2p_block_lead_time_minutes) --
        # `self.writer` is the union of both files' text now, same
        # substring-membership check as before, so a field genuinely read
        # by either half of the split integration still counts as read.
        # Generalised from that hand-rolled two-file union to the shared one,
        # which covers solver_writer.py plus EVERY module #1298 has extracted
        # out of it. Phase 2b/2c moved the quality scorer into
        # solver_reports/quality.py, taking solver_p2p_bonus_price and
        # solver_p2p_bonus_volume_kwh's only read sites with it -- so a
        # two-file union reported them as silently inert when they are read
        # exactly as before. See tests/_writer_source.py.
        self.writer = writer_source()

    def test_the_sweep_finds_a_real_set_of_fields(self):
        # test_the_sweep_finds_a_real_set_of_fields: Guards the guard: a regex that silently matched nothing would
        self.assertGreater(
            len(self.fields),
            30,
            "the number.py/const.py sweep found implausibly few solver "
            "fields -- the parsing has probably drifted, and this whole "
            "file would be passing on an empty set",
        )

        # test_no_solver_number_is_silently_inert
        inert = sorted(
            f for f in self.fields if f not in self.writer and f not in KNOWN_INERT
        )
        self.assertEqual(
            inert,
            [],
            "these dashboard-editable solver settings are never read by "
            "solver_writer.py, so changing them has ZERO effect on "
            f"dispatch or scoring: {inert}. This is the nimbus #538 / "
            "#1013 class -- a dial that looks live and does nothing. "
            "Either wire it into the solve, or add it to KNOWN_INERT "
            "with the issue number and a reason.",
        )

        # test_known_inert_fields_are_still_inert: Self-cleaning. When #1013 is fixed, this fails and says so,
        for field, reason in KNOWN_INERT.items():
            with self.subTest(field=field):
                self.assertNotIn(
                    field,
                    self.writer,
                    f"{field} is now read by solver_writer.py -- good. "
                    f"Delete its KNOWN_INERT entry. Recorded reason was: "
                    f"{reason}",
                )

        # test_known_inert_fields_still_exist_at_all: A renamed or removed field must not linger as a dead
        for field in KNOWN_INERT:
            with self.subTest(field=field):
                self.assertIn(
                    field,
                    self.fields,
                    f"{field} is in KNOWN_INERT but number.py no longer "
                    "offers it -- remove the stale entry",
                )


# --- test_diagnostic_log_levels ---------------------------------------------------------------
# Temporary per-issue diagnostic logging must not sit at WARNING on a path that
# runs every solve cycle.

_NIMBUS_DIR_diagnostic_log_levels = (
    Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
)


_SOLVER_WRITER = _NIMBUS_DIR_diagnostic_log_levels / "solver_writer.py"


# nimbus issue #1300: some of #757's own diag call sites (inside
# build_extra_batteries()) moved to solver_inputs/extra_batteries.py.
# Scanned as a union with solver_writer.py itself, same "every module
# #735/#1298 pulled logic out of" reasoning test_docs_writer_function_
# set_drift.py's own _integration_paths() already uses -- a moved
# diagnostic is still a diagnostic, and the guard below must keep seeing
# it wherever it currently lives.
#
# nimbus issue #1301 (Phase 2, spec 001, #1347): solver_shared.py joins
# the union for the identical reason -- it is where ha_get/ha_post_state/
# the other HA-I/O and config-resolution helpers now live, so a future
# "#N diag:" call site added to one of them must keep being seen here.
def _scanned_paths() -> list[Path]:
    return [
        _SOLVER_WRITER,
        _NIMBUS_DIR_diagnostic_log_levels / "solver_shared.py",
        # nimbus #1304 (spec 005 step 5): publish_plan and its thirteen helpers
        # moved here, and six of them carry _LOGGER calls -- so a future
        # "#N diag:" line added to one of them must keep being seen.
        _NIMBUS_DIR_diagnostic_log_levels / "solver_publish.py",
    ] + [
        Path(p)
        for p in sorted(
            glob.glob(str(_NIMBUS_DIR_diagnostic_log_levels / "solver_inputs" / "*.py"))
        )
    ]


# Matches the project's own temporary-instrumentation convention, e.g.
# "Nimbus #757 diag: ...".
_DIAG_RE = re.compile(r"#\d+ diag:")


_LOGGER_CALL_RE = re.compile(r"_LOGGER\.(debug|info|warning|error|critical)\s*\(")


# solver_inputs/*.py modules reach the shared logger via `sw._LOGGER`
# rather than the bare `_LOGGER` solver_writer.py itself uses -- see
# solver_inputs/__init__.py for why it's always accessed through the
# deferred `sw` module object, never imported by name.
_SW_LOGGER_CALL_RE = re.compile(
    r"sw\._LOGGER\.(debug|info|warning|error|critical)\s*\("
)


# How far back to look for the _LOGGER call that owns a message line --
# the message is often the first argument on a following line.
_LOOKBACK = 4


def _diag_call_levels() -> list[tuple[int, str, str]]:
    """(line_number, level, message_snippet) for every `#N diag:` line
    that can be attributed to a _LOGGER call just above it, across
    solver_writer.py and every solver_inputs/*.py module."""
    found: list[tuple[int, str, str]] = []
    for path in _scanned_paths():
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            if not _DIAG_RE.search(line):
                continue
            for back in range(idx, max(-1, idx - _LOOKBACK), -1):
                m = _LOGGER_CALL_RE.search(lines[back]) or _SW_LOGGER_CALL_RE.search(
                    lines[back]
                )
                if m:
                    found.append((idx + 1, m.group(1), line.strip()[:70]))
                    break
    return found


class TestTemporaryDiagnosticsAreNotWarnings(unittest.TestCase):
    def test_no_per_issue_diag_message_logs_at_warning_or_above(self):
        # test_no_per_issue_diag_message_logs_at_warning_or_above
        noisy = [
            (ln, lvl, msg)
            for ln, lvl, msg in _diag_call_levels()
            if lvl in ("warning", "error", "critical")
        ]
        self.assertEqual(
            noisy,
            [],
            "temporary '#N diag:' instrumentation is logging at WARNING or "
            f"above on a per-solve path: {noisy}. That is how #757's own "
            "diagnostics grew to 55% of all log volume and blocked an "
            "investigation into #757 itself. Use _LOGGER.debug() -- the "
            "messages stay available to anyone raising the log level, "
            "without consuming the default window for everyone else.",
        )

        # test_the_scan_actually_finds_the_known_diag_messages: Guards the guard. If the `#N diag:` convention were renamed, or
        found = _diag_call_levels()
        self.assertGreaterEqual(
            len(found),
            5,
            f"expected to find the known #757 diag call sites, found "
            f"{len(found)} -- the scan is no longer attributing diag "
            f"messages to their _LOGGER calls, so this file enforces "
            f"nothing.",
        )

        # test_every_found_diag_is_attributed_to_a_real_level
        for ln, lvl, msg in _diag_call_levels():
            with self.subTest(line=ln, msg=msg):
                self.assertIn(lvl, ("debug", "info", "warning", "error", "critical"))


# --- test_flattened_prefix_suffix_collisions --------------------------------------------------
# Two flattened-attribute tables that share an `entity_id_prefix` must not
# define the same `entity_id_suffix` (nimbus issue #1192 follow-up).

_SRC_flattened_prefix_suffix_ = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "sensor_flattened.py"
)


def _source() -> str:
    """`read_text`, not `open(...).read()`.

    The latter leaks the handle, and CI's pytest promotes the resulting
    ResourceWarning to an error -- so all five tests failed there while
    passing under a local `unittest` run, which does not. The regex and
    the assertions were never the problem.
    """
    return _SRC_flattened_prefix_suffix_.read_text(encoding="utf-8")


def _factories(src: str) -> list[tuple[str, str, str]]:
    """(factory_name, entity_id_prefix, table_name) discovered from source."""
    return re.findall(
        r"def (create_flattened_entities_\w+)\(.*?"
        r'entity_id_prefix="([^"]+)".*?'
        r"for spec in (FLATTENED_ATTRS_\w+)",
        src,
        re.DOTALL,
    )


def _suffixes(src: str, table: str) -> set[str]:
    i = src.index(table + ":")
    j = src.index("\n)", i)
    return set(re.findall(r'entity_id_suffix="([^"]+)"', src[i:j]))


class TestTheDiscoveryItselfWorks(unittest.TestCase):
    """A guard built on a regex that silently matches nothing would pass
    forever while checking nothing. Pin the discovery before trusting it.
    """

    def test_it_finds_the_known_factories(self):
        # test_it_finds_the_known_factories
        facs = _factories(_source())
        self.assertGreaterEqual(
            len(facs),
            5,
            "expected at least the five known flattened factories; the "
            f"discovery regex found {len(facs)} -- it has probably stopped "
            "matching rather than the factories having gone away",
        )

        # test_every_discovered_table_yields_suffixes
        src = _source()
        for name, _prefix, table in _factories(src):
            with self.subTest(factory=name, table=table):
                self.assertTrue(
                    _suffixes(src, table),
                    f"{table} parsed to zero suffixes -- the parse is "
                    "broken, not the table",
                )

        # test_the_known_shared_prefix_is_still_shared: If this ever fails because `nimbus_flex` stopped being shared,
        by_prefix = collections.defaultdict(list)
        for _name, prefix, table in _factories(_source()):
            by_prefix[prefix].append(table)
        shared = {p: t for p, t in by_prefix.items() if len(t) > 1}
        self.assertIn(
            "nimbus_flex",
            shared,
            "nimbus_flex was the only prefix shared by two tables when "
            f"this guard was written; shared prefixes now: {shared}",
        )


class TestNoSuffixCollisionWithinAPrefix(unittest.TestCase):
    """The actual invariant."""

    def test_tables_sharing_a_prefix_have_disjoint_suffixes(self):
        # test_tables_sharing_a_prefix_have_disjoint_suffixes
        src = _source()
        by_prefix: dict[str, list[str]] = collections.defaultdict(list)
        for _name, prefix, table in _factories(src):
            by_prefix[prefix].append(table)

        for prefix, tables in sorted(by_prefix.items()):
            if len(tables) < 2:
                continue
            sets = {t: _suffixes(src, t) for t in tables}
            for a_i, a in enumerate(tables):
                for b in tables[a_i + 1 :]:
                    clash = sets[a] & sets[b]
                    with self.subTest(prefix=prefix, a=a, b=b):
                        self.assertEqual(
                            clash,
                            set(),
                            f"{a} and {b} both use entity_id_prefix "
                            f"'{prefix}' and both define {sorted(clash)} -- "
                            "those children would compete for one "
                            "entity_id and HA would bump one to _2. Give "
                            "one table its own prefix, or rename the "
                            "suffix.",
                        )

        # test_suffixes_are_unique_within_each_table_too: A table that repeats a suffix collides with itself, which no
        src = _source()
        for _name, _prefix, table in _factories(src):
            i = src.index(table + ":")
            j = src.index("\n)", i)
            found = re.findall(r'entity_id_suffix="([^"]+)"', src[i:j])
            dupes = [s for s, n in collections.Counter(found).items() if n > 1]
            with self.subTest(table=table):
                self.assertEqual(dupes, [], f"{table} repeats {dupes}")


# --- test_config_surface_budget ---------------------------------------------------------------
# nimbus issue #448: guard the config-surface counts the architecture proposal
# argues from, so they cannot go stale.

_HUB_OPTIONS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "flows"
    / "hub_options.py"
)


# Per-form totals, from each form's own key tuple.
EXPECTED_KEY_COUNTS = {
    "_SWITCHBOARD_SCHEMA_KEYS": 8,
    "_FORECASTER_SCHEMA_KEYS": 12,
    # nimbus issue #1213 (Mark Purcell): 26 -> 27, one OPTIONAL field,
    # `solver_price_event_sensor`. #449's standing question was asked
    # before adding it, and the answer is in the commit: the field names
    # an ENTITY, so it cannot be auto-discovered (its magnitude and
    # windows are the household's deliberate choice, not something to
    # find), and the two parts that COULD live on the device already do
    # -- the arm is switch.nimbus_solver_price_event_enabled, and the
    # magnitude/windows live in the household's own sensor rather than
    # in Nimbus config at all. The alternative shape (numbers plus
    # datetimes for one window) would cost MORE config surface and
    # support fewer window shapes than the step-point sensor #1213 asks
    # for.
    # nimbus #1537 item 3: 27 -> 28, one OPTIONAL field,
    # `solver_p2p_matched_rate_forecast_sensor_2`. #449's question: it names
    # an ENTITY, so it cannot be a device config entity; where it CAN be
    # discovered it is -- on LocalVolts v2 it is pre-filled from Sell P2P
    # Matched Cost by unique_id (pricing_autodetect.LV_V2_PROFILE). Reusing
    # the first field for both sources would need a multi-entity selector
    # and change that field's saved shape on every install.
    # nimbus #1786: 28 -> 29, one OPTIONAL field,
    # `solver_price_comparison_sensors`: price sources recorded with the
    # decision inputs for scoring consolidation rules, never read by the
    # solve. #449's question: auto-discovery was checked first and cannot
    # serve the install Mark named ("devhub should have all these price
    # sources configured"): there, Amber Express and AEMO NEM Data reach HA
    # through remote_homeassistant, so their registry platform is the mirror
    # (measured 18 Oct) and pricing_autodetect's provider lookup finds
    # neither. It names entities, so it cannot be a device config entity.
    # nimbus #1786 (Mark): 29 -> 31, two OPTIONAL fields,
    # `solver_loss_factor_tni_sensor` and `solver_loss_factor_dlf_code_sensor`:
    # the sensors holding the site's TNI and DLF code, whose loss factors are
    # then looked up in AEMO's tables. #449's question: they name entities,
    # so they cannot be device config entities, and where they CAN be
    # discovered they are -- on LocalVolts v2 they are pre-filled from its
    # `metadata_tni` / `metadata_dlf` sensors by unique_id
    # (pricing_autodetect.LV_V2_OPTIONAL_PROFILE), as #1537 did. The factor
    # values themselves are never a field: they come from AEMO's tables.
    "_SOLVER_WIZARD_SCHEMA_KEYS": 31,
}


# (required, optional) per schema builder. `_switchboard_schema` reads as
# (0, 1) on purpose -- see this module's docstring: its eight fields come
# from one call inside a loop. That row is the counting trap, kept visible
# rather than hidden behind a number that looks right.
EXPECTED_SPLIT = {
    "_forecaster_schema": (0, 12),
    "_solver_battery_schema": (1, 1),
    # nimbus issue #1213: 6 -> 7 optional. The two Required price fields
    # are unchanged, which is the property this row exists to watch.
    # Household, 20 Oct 2026: 7 -> 11 optional, a MOVE, not an addition:
    # the AEMO price source (regional forecast, price now, TNI, DLF code)
    # joined the other price sources as source 2; the Sources step lost the
    # same four. No key changed name, and every key is still in
    # _SOLVER_WIZARD_SCHEMA_KEYS, so the final merge takes it whichever
    # step submits it.
    "_solver_grid_schema": (2, 11),
    # nimbus #1537: 14 -> 15 optional (the second P2P matched-rate source).
    # nimbus #1786: 15 -> 16 optional (comparison-only price sources).
    # nimbus #1786: 16 -> 18 optional (the site's TNI and DLF code sensors).
    # Household, 20 Oct 2026: 18 -> 14, the four AEMO fields moved to the
    # Grid Prices step (above).
    "_solver_sources_schema": (2, 14),
    "_switchboard_schema": (0, 1),
}


_WHEN_THIS_FIRES = (
    "\n\nThis is not a failure -- it is the prompt #448 asked for. The "
    "config surface changed, so decide on purpose and update the number "
    "in the same commit:\n"
    "  * ADDED a field? Ask Mark's standing question first (#449): could "
    "this be auto-discovered, or a sensible default surfaced as a "
    "config entity on the device instead? Three fields have been retired "
    "that way already (#495, #452, #768).\n"
    "  * RETIRED a field? Lower the number and say so -- that is the "
    "direction this guard exists to make visible, and it is the one "
    "nobody currently counts.\n"
    "  * Restructured a form? Check no key changed which step owns it: "
    "that is the wipe class four separate dated fixes in "
    "async_step_forecaster already exist to protect against."
)


def _tree() -> ast.Module:
    return ast.parse(_HUB_OPTIONS.read_text(encoding="utf-8"))


def _key_counts(tree: ast.Module) -> dict[str, int]:
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id.endswith("_SCHEMA_KEYS")
                and isinstance(node.value, (ast.Tuple, ast.List, ast.Set))
            ):
                out[target.id] = len(node.value.elts)
    return out


def _split(tree: ast.Module) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        req = opt = 0
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                if sub.func.attr == "Required":
                    req += 1
                elif sub.func.attr == "Optional":
                    opt += 1
        if req or opt:
            out[fn.name] = (req, opt)
    return out


class TestTheConfigSurfaceIsMeasured(unittest.TestCase):
    def test_per_form_field_counts(self):
        # test_per_form_field_counts
        actual = _key_counts(_tree())
        self.assertEqual(
            {k: actual.get(k) for k in EXPECTED_KEY_COUNTS},
            EXPECTED_KEY_COUNTS,
            "a form's field count changed." + _WHEN_THIS_FIRES,
        )

        # test_the_total_a_household_actually_meets: 46 across three forms, before any subentry. #448 had been
        actual = _key_counts(_tree())
        total = sum(actual.get(k, 0) for k in EXPECTED_KEY_COUNTS)
        self.assertEqual(
            total,
            sum(EXPECTED_KEY_COUNTS.values()),
            f"the whole config surface is now {total} fields, not "
            f"{sum(EXPECTED_KEY_COUNTS.values())}." + _WHEN_THIS_FIRES,
        )

        # test_required_versus_optional_split_per_form: The interesting half. A form rendering 16 fields of which 2 are
        actual = _split(_tree())
        self.assertEqual(
            {k: actual.get(k) for k in EXPECTED_SPLIT},
            EXPECTED_SPLIT,
            "a form's required/optional split changed." + _WHEN_THIS_FIRES,
        )

        # test_only_five_fields_are_genuinely_required: The number worth quoting, and the one nobody had: the wizard
        actual = _split(_tree())
        required = sum(r for r, _o in actual.values())
        self.assertEqual(
            required,
            5,
            f"the number of genuinely required fields is now {required}, "
            f"not 5. If that went UP, a household's minimum setup just got "
            f"harder -- the one direction #448 is most concerned with."
            + _WHEN_THIS_FIRES,
        )


class TestTheCountingTrapStaysDocumented(unittest.TestCase):
    """The reason this file parses instead of grepping. Kept as a test so
    the discrepancy is demonstrated rather than asserted in prose."""

    def test_switchboard_has_eight_keys_but_one_optional_call(self):
        tree = _tree()
        keys = _key_counts(tree)["_SWITCHBOARD_SCHEMA_KEYS"]
        _req, opt_calls = _split(tree)["_switchboard_schema"]
        self.assertEqual(keys, 8)
        self.assertEqual(
            opt_calls,
            1,
            "the switchboard schema no longer builds its fields in a loop. "
            "That is fine -- but this test existed to demonstrate that "
            "counting vol.Optional() calls undercounts it 8:1, so if the "
            "loop is gone, delete this test rather than 'fixing' the "
            "number, and keep the docstring's warning in "
            "test_gates_source_and_docs.py's module header.",
        )


# --- test_vendored_telemetry_schema -----------------------------------------------------------
# nimbus issues #495 / #496: the vendored `nem-flex-telemetry` schema.

_REPO_ROOT_vendored_telemetry_schem = Path(__file__).resolve().parent.parent


_SCHEMA_PATH = _REPO_ROOT_vendored_telemetry_schem / "schema" / "telemetry.schema.json"


# Recorded in schema/PROVENANCE.md; both must move together.
_PINNED_COMMIT = "024f45c61b3f4a6cb251326f0b15955a9556d3ed"


_PINNED_SHA256 = "24478a75be3cbaae53b1ff613b911483149e8362608f83bcd29078dd3db91f35"


_RAW_URL = (
    "https://raw.githubusercontent.com/purcell-lab/nem-flex-telemetry/"
    "main/schema/telemetry.schema.json"
)


# The fields Nimbus's own emitter is specified against (#495's field
# table). Pinned as a set rather than a count so a swap -- one field
# removed and another added -- cannot pass a length check.
_REQUIRED = {
    "schema_version",
    "interval_start_utc",
    "region",
    "postcode_prefix",
    "net_import_kw",
    "solar_kw",
    "house_load_kw",
    "deferrable_load_kw",
    "naive_baseline_kw",
    "naive_baseline_method",
    "price_signal_seen",
    "price_export_seen",
    "envelope_import_limit_kw",
    "envelope_export_limit_kw",
    "flex_available_up_kw",
    "flex_available_down_kw",
    "shadow_energy_price",
    "shadow_load_forecast_price",
    "shadow_solar_forecast_price",
}


def _schema():
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


class TestTheVendoredCopyIsIntact(unittest.TestCase):
    """Catches a local edit to a file this repo does not own."""

    def test_the_file_exists_where_495_expects_it(self):
        # test_the_file_exists_where_495_expects_it
        self.assertTrue(
            _SCHEMA_PATH.is_file(),
            "#495 is blocked on exactly this file being present",
        )

        # test_the_bytes_match_what_was_vendored: The whole point of a vendored copy is that it is the upstream
        got = hashlib.sha256(_SCHEMA_PATH.read_bytes()).hexdigest()
        self.assertEqual(got, _PINNED_SHA256)

        # test_it_is_parseable_json
        self.assertIsInstance(_schema(), dict)


class TestItIsTheSchemaNumber495Describes(unittest.TestCase):
    """Identity checks against what #495's own body and Mark's own
    confirmation state, so a re-pin to a genuinely different schema
    cannot pass as a routine version bump."""

    def test_draft_and_version(self):
        # test_draft_and_version
        s = _schema()
        self.assertEqual(s["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(str(s["version"]), "2.0")

        # test_additional_properties_is_closed_at_the_top_level: #495 calls this out specifically: the record is validated with
        self.assertFalse(_schema()["additionalProperties"])

        # test_schema_version_is_a_const
        self.assertEqual(str(_schema()["properties"]["schema_version"]["const"]), "2.0")

        # test_every_required_field_495_maps_is_present
        s = _schema()
        required = set(s["required"])
        missing = _REQUIRED - required
        self.assertEqual(
            missing,
            set(),
            "fields #495's own source table maps are no longer required upstream",
        )
        # Every required field must also be declared, or the schema
        # cannot validate anything against it.
        self.assertEqual(required - set(s["properties"]), set())

        # test_the_settled_naive_baseline_method_is_still_in_the_enum: `subtraction` was Mark's own call on 2026-09-13, chosen
        enum = _schema()["properties"]["naive_baseline_method"]["enum"]
        self.assertIn("subtraction", enum)

        # test_region_and_postcode_prefix_are_the_shapes_nimbus_shipped_for: These two were genuinely new config (shipped v0.94.299/300)
        props = _schema()["properties"]
        self.assertIn("QLD1", props["region"]["enum"])
        self.assertEqual(props["postcode_prefix"]["pattern"], "^[0-9]{3}$")


@unittest.skipUnless(
    os.environ.get("NIMBUS_SCHEMA_DRIFT_CHECK") == "1",
    "online drift check -- set NIMBUS_SCHEMA_DRIFT_CHECK=1 to run it. Off by "
    "default so this repo's CI does not depend on another repo's uptime.",
)
class TestItHasNotDriftedUpstream(unittest.TestCase):
    """The periodic half of Mark's 'recorded ref + drift check'. Run when
    picking up #495/#496, or on a schedule."""

    def test_upstream_main_still_matches_the_pinned_copy(self):
        import urllib.request

        with urllib.request.urlopen(_RAW_URL, timeout=30) as r:
            upstream = r.read()
        self.assertEqual(
            hashlib.sha256(upstream).hexdigest(),
            _PINNED_SHA256,
            f"upstream telemetry.schema.json has moved since "
            f"{_PINNED_COMMIT[:12]}. This is not automatically a problem -- "
            f"re-read the upstream diff, decide whether Nimbus's emitter "
            f"needs changes, then re-pin the commit, the sha256 and "
            f"schema/PROVENANCE.md together.",
        )


# --- test_1067_section_requires_a_flat_save_path ----------------------------------------------
# nimbus #1067: a config-flow `section` cannot be added until the save paths
# stop assuming flat `user_input`.

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


FLOW_DIR = REPO_ROOT / "custom_components" / "nimbus_load" / "flows"


CONFIG_FLOW = REPO_ROOT / "custom_components" / "nimbus_load" / "config_flow.py"


# The flat-key merge, verbatim. Both hub-options steps use it.
_FLAT_MERGE = "merged[key] = user_input.get(key)"


# The subentry flows' wholesale persist.
_WHOLESALE = "data=user_input"


def _flow_files() -> list[pathlib.Path]:
    files = [p for p in FLOW_DIR.glob("*.py") if p.name != "__init__.py"]
    if CONFIG_FLOW.exists():
        files.append(CONFIG_FLOW)
    return sorted(files)


def _imports_section(path: pathlib.Path) -> bool:
    """Whether this module imports HA's `section` helper.

    AST rather than a substring search: "section" appears in ordinary
    prose and attribute names throughout these files, and a guard that
    fires on a comment would be retired the first time it did.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and "data_entry_flow" in node.module
            and any(a.name == "section" for a in node.names)
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "section":
            # nimbus issue #1187 (Mark Purcell): walk an arbitrary-depth
            # attribute chain back to its root `ast.Name`, rather than
            # requiring exactly one level.
            #
            # The original `isinstance(value, ast.Name)` test only saw a
            # 2-level chain -- `data_entry_flow.section(...)` after
            # `from homeassistant import data_entry_flow`. A plain
            # `import homeassistant.data_entry_flow` followed by
            # `homeassistant.data_entry_flow.section(...)` puts another
            # `ast.Attribute` in `node.value`, so the guard returned
            # False and the section was invisible to it.
            #
            # That is ordinary Python, not a contrived shape. This
            # codebase happens to use `from X import Y` exclusively
            # today (zero `^import homeassistant.` hits), but nothing
            # enforces that, and a future flow file written the other
            # way would slip past the guard while genuinely triggering
            # the flat-save bug it exists to catch.
            parts = []
            value = node.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                parts.append(value.id)
                if any("data_entry_flow" in part for part in parts):
                    return True
    return False


# nimbus #1067: hub_options.py is now section-aware and is the ONE allowed
# user of `section`. This guard was written to fire on exactly that change,
# and it did -- so it is narrowed here rather than deleted, because the
# reason it existed still applies everywhere else.
#
# WHAT MADE hub_options SAFE, and it is only safe for the SOLVER steps:
# `_absorb_step()` now calls `_flatten_section_input()` before merging and
# `_effective_schema_keys()` for its null sweep, so `self._solver_data` is
# flat. The Solver save loop reads THAT (`merged[key] = self._solver_data
# .get(key)`), never `user_input`, so a nested submission is already
# unwrapped by the time it is persisted.
#
# The Forecaster and Switchboard steps in the same file still read
# `user_input.get(key)` DIRECTLY, and the six subentry flows still persist
# `data=user_input` wholesale. Sectioning any of those would reproduce the
# original bug exactly, which is what the two tests below now pin.
_SECTION_ALLOWED = {"hub_options.py"}


class TestOnlyTheFlattenedPathUsesASection(unittest.TestCase):
    def test_no_unflattened_flow_module_imports_the_section_helper(self):
        # test_no_unflattened_flow_module_imports_the_section_helper
        offenders = [
            p.name
            for p in _flow_files()
            if _imports_section(p) and p.name not in _SECTION_ALLOWED
        ]
        self.assertEqual(
            offenders,
            [],
            f"{offenders} import Home Assistant's `section` helper while "
            "their save path still assumes flat `user_input`. A section "
            "nests its fields one level down, so the subentry flows' "
            f"`{_WHOLESALE}` stores the nested dict instead of the values, "
            "and a flat key loop writes None for every field inside it. "
            "Neither raises. Flatten that save path FIRST, as its own "
            "change with tests, then add the section and widen "
            "_SECTION_ALLOWED in the same commit -- see nimbus issue #1067.",
        )

        # test_hub_options_only_qualifies_because_it_flattens: The allowance is conditional, not a permanent exemption. If the
        text = (FLOW_DIR / "hub_options.py").read_text(encoding="utf-8")
        for required in (
            "_flatten_section_input",
            "_effective_schema_keys",
            "merged[key] = self._solver_data.get(key)",
        ):
            with self.subTest(required=required):
                self.assertIn(
                    required,
                    text,
                    "hub_options.py uses a section, and this is what makes "
                    "that safe. Removing it reopens #1067's silent-null bug.",
                )

        # test_the_forecaster_and_switchboard_steps_are_NOT_sectioned: Both still read `user_input.get(key)` directly, so neither may be
        text = (FLOW_DIR / "hub_options.py").read_text(encoding="utf-8")
        for step in ("async_step_forecaster", "async_step_switchboard"):
            i = text.index(f"async def {step}(")
            # the step body runs until the next method definition
            j = text.index(chr(10) + "    async def ", i + 10)
            with self.subTest(step=step):
                self.assertNotIn(
                    "_collapse_optionals_into_advanced",
                    text[i:j],
                    f"{step} saves via `user_input.get(key)`, so sectioning "
                    "it would write None for every optional field. Flatten "
                    "its save path first (#1067).",
                )


class TestTheGuardIsNotVacuous(unittest.TestCase):
    """If neither save pattern still exists, this guard is protecting
    nothing and should be re-read rather than left passing."""

    def test_the_flat_merge_is_still_there(self):
        # test_the_flat_merge_is_still_there
        text = (FLOW_DIR / "hub_options.py").read_text(encoding="utf-8")
        self.assertIn(
            _FLAT_MERGE,
            text,
            "hub_options.py no longer uses the flat-key merge this guard "
            "exists for. If the save path is now section-aware, relax the "
            "guard deliberately rather than leaving it asserting nothing.",
        )

        # test_the_flat_merge_is_used_by_both_steps: Two steps share it. A fix that only reached one would leave the
        text = (FLOW_DIR / "hub_options.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count(_FLAT_MERGE), 2)

        # test_the_wholesale_persist_is_still_there
        hits = [
            p.name for p in _flow_files() if _WHOLESALE in p.read_text(encoding="utf-8")
        ]
        self.assertTrue(
            hits,
            "no flow persists `data=user_input` wholesale any more -- the "
            "second half of this guard is protecting nothing.",
        )

        # test_there_are_flow_modules_to_check_at_all: A rename or a move that emptied the glob would make every
        self.assertGreater(len(_flow_files()), 5)


class TestTheDetectorItself(unittest.TestCase):
    """The detector is the part that can silently stop working."""

    def test_it_finds_a_from_import(self):
        # test_it_finds_a_from_import
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("from homeassistant.data_entry_flow import section\n")
            path = pathlib.Path(fh.name)
        self.assertTrue(_imports_section(path))

        # test_it_finds_an_attribute_use
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("import homeassistant.data_entry_flow as data_entry_flow\n")
            fh.write("x = data_entry_flow.section(schema)\n")
            path = pathlib.Path(fh.name)
        self.assertTrue(_imports_section(path))

        # test_prose_and_attribute_names_do_not_trip_it: "section" is an ordinary English word and appears throughout
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("# see the section above about sections\n")
            fh.write('SECTION = "advanced"\n')
            fh.write('def section_title(): return "x"\n')
            path = pathlib.Path(fh.name)
        self.assertFalse(_imports_section(path))


# --- test_1187_section_guard_misses_fully_qualified_import ------------------------------------
# The section detector also sees a fully qualified `homeassistant.data_entry_flow.section(...)`
# (#1187).


def test_the_section_detector_sees_a_fully_qualified_import(tmp_path):
    # test_the_section_detector_sees_a_fully_qualified_import
    tmp_0 = tmp_path / "test_the_section_detector_sees_a_fully_qualified_import"
    tmp_0.mkdir()
    path = tmp_0 / "flow.py"
    path.write_text(
        "import homeassistant.data_entry_flow\n\n\n"
        "def foo():\n"
        '    return homeassistant.data_entry_flow.section({}, {"collapsed": True})\n',
        encoding="utf-8",
    )
    assert _imports_section(path)

    # test_1213_both_string_files_describe_the_price_event_field
    # test_1213_both_string_files_describe_the_price_event_field
    base = _REPO_ROOT / "custom_components" / "nimbus_load"
    for name in ("strings.json", "translations/en.json"):
        blob = json.loads((base / name).read_text(encoding="utf-8"))
        dumped = json.dumps(blob)
        assert "solver_price_event_sensor" in dumped, name
        assert "value of 0" in dumped, name
        assert "holds forward" in dumped, name
        desc = blob["options"]["step"]["solver_grid"]["data_description"]
        assert "{" not in desc["solver_price_event_sensor"], name

    # test_1179_the_solver_never_branches_on_the_weight_it_reports
    for name in ("lp.py", "network.py"):
        hits = [
            t for t in _conditions(_SOLVER_DIR / name) if "calibration_weight_used" in t
        ]
        assert hits == [], f"{name} branches on the reported weight: {hits}"

    # test_1179_the_writer_branches_on_the_highs_diagnostics_only_to_log_them
    hits = [
        t
        for path in writer_source_paths()
        for t in _conditions(path)
        if "highs_info" in t or "failing_model_path" in t
    ]
    assert hits == ["plan.failing_model_path is not None"], hits

    # test_1179_sensor_py_names_how_many_solve_diagnostics_keys_there_are
    keys = None
    for _path, tree in writer_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "solve_diagnostics"
                        and isinstance(v, ast.Dict)
                    ):
                        keys = [c for c in v.keys if isinstance(c, ast.Constant)]
    assert keys is not None, "no solve_diagnostics dict literal in the writer"
    words = [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
    ]
    words += [f"twenty-{w}" for w in words[1:10]]
    doc = (_REPO_ROOT / "custom_components" / "nimbus_load" / "sensor.py").read_text(
        encoding="utf-8"
    )
    assert f"emits **{words[len(keys)]}**" in doc, len(keys)

    # test_the_cron_copy_stamps_the_row_before_it_is_frozen
    src = _CRON.read_text(encoding="utf-8")
    assert "def _nimbus_version()" in src
    assert '_QUALITY_HISTORY_VERSION_FIELD = "v"' in src
    stamp = src.index("day_entry[_QUALITY_HISTORY_VERSION_FIELD] = _version")
    assert stamp < src.index("quality_history[day_key] = day_entry")


# --- test_1213_the_price_event_is_wired -------------------------------------------------------
# #1213: the price event field's description states the close-your-window
# requirement, the one mistake that turns a test into a lasting distortion,
# in both string files. Asserted on two load-bearing phrases, and never on a
# brace pair, which hassfest reads as a placeholder and rejects.


# --- test_1179_the_diagnostics_steer_nothing --------------------------------------------------
# #1179: the blend weight, HiGHS's own numbers and the dumped model are
# observability only. The solve never branches on the weight it reports, the
# writer branches on the diagnostics exactly once (whether to print a path it
# has), and sensor.py's staleness fingerprint names how many keys
# `solve_diagnostics` emits. The fallback flag and reason are tested by
# behaviour in tests/v2/solver/test_lp_failure_paths.py.

_SOLVER_DIR = _REPO_ROOT / "custom_components" / "nimbus_load" / "solver"


def _conditions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        ast.unparse(n.test)
        for n in ast.walk(tree)
        if isinstance(n, (ast.If, ast.While, ast.IfExp))
    ]


# --- test_1120_the_cron_copy_stamps_its_rows --------------------------------------------------
# nimbus #1120: the standalone (cron) quality writer stamps the row it writes
# with the release that scored it, as the native writer does. A check that
# reads source text, because the cron copy cannot be imported here; kept apart
# until the decision on source-reading tests (#1837). The native stamp is in
# tests/v2/reports/test_daily_reports.py.

_CRON = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "real-world-integration"
    / "files"
    / "nimbus_solver_quality_writer.py"
)


# --- test_125_the_cron_copy_labels_the_same ---------------------------------------------------
# nimbus #125/#133: the standalone writer carries the same binding-constraint
# branch as the native one. A check that reads the cron copy's source, kept
# apart until the decision on source-reading tests (#1837); the label itself is
# in tests/v2/orchestration/test_binding_constraint_label.py.


class TestBothWriterCopiesStayInSync(unittest.TestCase):
    def test_the_standalone_writer_has_the_same_new_branch(self):
        import pathlib

        standalone = (
            pathlib.Path(__file__).resolve().parent.parent
            / "docs"
            / "real-world-integration"
            / "files"
            / "nimbus_solver_forecast_writer.py"
        ).read_text(encoding="utf-8")
        self.assertIn("by P2P export commitment", standalone)
        self.assertIn("fixed_export_kw_now", standalone)


# --- test_923_the_cron_copy_compares_the_gated_battery ----------------------------------------
# nimbus #923: the standalone writer compares the gated battery, not the fleet
# aggregate, in its fixed-export charge backstop. A check that reads the cron
# copy's source, kept apart until the decision on source-reading tests (#1837);
# the clamp itself is in
# tests/v2/orchestration/test_fixed_export_charge_clamp.py.

NAN = float("nan")


class TestBothWriterCopiesStayInSync_923_the_cron_copy_compar(unittest.TestCase):
    def test_the_standalone_writer_also_compares_the_gated_battery(self):
        import pathlib

        standalone = (
            pathlib.Path(__file__).resolve().parent.parent
            / "docs"
            / "real-world-integration"
            / "files"
            / "nimbus_solver_forecast_writer.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def resolve_fixed_export_charge_clamp(", standalone)
        self.assertIn("plan.batteries[0].charge_kw", standalone)
        self.assertNotIn(
            "_violation_mask = _fixed_mask & (plan.battery_charge_kw > 0.05)",
            standalone,
        )


# --- test_1994_no_external_entity_id_in_code --------------------------------------------------
# nimbus #1994: no external entity_id is written into the integration's code;
# every external source is found through the registry (`known_sources`) or
# configured. A gate that reads the package source, kept apart until the
# decision on source-reading tests (#1837); the registry lookup is in
# tests/v2/setup/test_known_sources.py.

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"


_ENTITY_ID = re.compile(
    r"^(?:sensor|binary_sensor|switch|number|select|input_number|input_boolean|"
    r"input_select|weather|water_heater|climate|button|time|device_tracker|"
    r"person|sun|zone)\.[a-z0-9_]+$"
)


def _docstring_ids(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            out.add(id(body[0].value))
    return out


def test_no_external_entity_id_is_written_into_the_integration():
    """A string constant that is a whole entity_id, outside a docstring, must
    be one of Nimbus's own. Anything else is found through the registry
    (`known_sources`) or configured."""
    found = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and _ENTITY_ID.match(node.value)
                and ".nimbus" not in node.value
            ):
                found.append(f"{path.relative_to(PKG)}:{node.lineno} {node.value}")
    assert found == []


# --- test_582_one_same_day_window_helper ------------------------------------------------------
# nimbus #582: the same-day-window fix lives in one helper, not four copies
# (extracted under #485). Checks that read the source tree, kept apart until
# the decision on source-reading tests (#1837); the helper's behaviour is in
# tests/v2/orchestration/test_controllable_load_build.py.


class TestTheCopiesAreGone(unittest.TestCase):
    """The point of the extraction. Source-checked because the defect
    class is duplication itself, which no behavioural test can see."""

    def test_the_helper_has_exactly_four_callers(self):
        # test_the_helper_has_exactly_four_callers: Counted over the AST's real references, not the source text.
        import ast

        from _writer_source import writer_trees

        defined = 0
        called = 0
        for _path, tree in writer_trees():
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "_earliest_period_for_same_day_window"
                ):
                    defined += 1
                elif (
                    isinstance(node, ast.Name)
                    and node.id == "_earliest_period_for_same_day_window"
                    and isinstance(node.ctx, ast.Load)
                ):
                    called += 1
                elif (
                    isinstance(node, ast.Attribute)
                    and node.attr == "_earliest_period_for_same_day_window"
                    and isinstance(node.ctx, ast.Load)
                ):
                    # nimbus #1305 (spec 006, Phase 6): the two call sites inside
                    # apply_commanded_state_guard moved to solver_dispatch/guard.py
                    # and now reach this helper through the deferred seam, as
                    # `sw._earliest_period_for_same_day_window(...)` -- an
                    # ast.Attribute, not an ast.Name. Counting only Names would
                    # have silently dropped them and reported 2 of 4, i.e. this
                    # guard would have started under-counting rather than failing.
                    # `sw` itself is an ast.Name with a different id, so there is
                    # no double count.
                    called += 1
        self.assertEqual(defined, 1, "expected exactly one definition")
        self.assertEqual(
            called,
            4,
            "expected exactly four call sites (two in build_controllable_loads, "
            "two in apply_commanded_state_guard) -- a fifth copy of this logic "
            "reappearing inline is the exact drift #485 asked about, and it has "
            "already cost this project one real inconsistency",
        )

        # test_no_inline_copy_of_the_predicate_remains_in_those_functions: `earliest_today` should now appear only inside the helper and
        import ast

        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if fn.name in (
                "_earliest_period_for_same_day_window",
                "_build_daily_adequacy_windows",
            ):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Store)
                    and node.id == "earliest_today"
                ):
                    offenders.append(f"{fn.name}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "an inline copy of #582's same-day computation has reappeared "
            f"outside the helper: {offenders}. Call "
            "_earliest_period_for_same_day_window() instead -- four copies "
            "is how this started.",
        )


class TestTheFifthSiteIsNotACopy(unittest.TestCase):
    """`_build_daily_adequacy_windows()` shares the predicate but is a
    superset, and is deliberately excluded. Pinned so nobody folds it in
    later on the strength of a grep, and so that if it ever IS reduced to
    the same shape, this test says why that would now be safe.
    """

    def test_it_handles_cases_the_helper_does_not(self):

        text = writer_source()
        start = text.index("def _build_daily_adequacy_windows(")
        end = text.index("def ", start + 10)
        body = text[start:end]
        for marker in ("today_done", "now > deadline_today", "day_target_kwh"):
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    "_build_daily_adequacy_windows() no longer handles the "
                    "cases that made it a superset rather than a copy of "
                    "the #582 guard. If it has genuinely been reduced to "
                    "the same shape, it can now call "
                    "_earliest_period_for_same_day_window() -- and should.",
                )


# --- test_919_the_benchmark_docs_name_the_real_importers --------------------------------------
# nimbus #919: docs/reference-benchmark.md names exactly the modules that
# import forecast_regret, and tells a reader the field number is one step
# ahead. Checks that read the package source and the docs, kept apart until the
# decision on source-reading tests (#1837); the nowcast skill itself is in
# tests/v2/solver/test_nowcast_skill.py.

N = 24  # one day, hourly periods


START = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


class TestTheBenchmarkDocsClaimStaysTrue(unittest.TestCase):
    """`docs/reference-benchmark.md` makes a factual claim about who
    calls `compute_forecast_regret()`, and that claim went stale silently
    the moment this module started calling it.

    It had said the HACS integration "never calls it -- nothing outside
    `custom_components/nimbus_load/solver/` even imports
    `forecast_regret`". Nothing asserted that, so nothing caught it. This
    is the same stale-documented-claim class as the four guards added on
    2026-09-15 (entity names in a docs table, test-file names in source
    comments, units, and physics in a docstring) -- a claim a reader will
    trust needs something that fails when it stops being true.
    """

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_the_documented_importer_set_is_the_real_one(self):
        # test_the_documented_importer_set_is_the_real_one: Measures real IMPORT statements, not textual mentions. The
        importers = {
            path.name
            for path in (self.repo / "custom_components" / "nimbus_load").rglob("*.py")
            if re.search(
                r"^\s*(?:from\s+\.?\S*forecast_regret\s+import|import\s+\S*forecast_regret)",
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        }
        self.assertEqual(
            importers,
            # solver_writer.py joined on 2026-09-26 (#937 stage 3): it calls
            # compute_forecast_regret() directly for the DAY-AHEAD
            # decomposition, alongside nowcast_skill.py's one-step-ahead
            # measurement. The doc's caller table now records the horizon of
            # each, because that is what decides whether a field number is
            # comparable to this benchmark's.
            {"nowcast_skill.py", "reference_benchmark.py", "solver_writer.py"},
            "the set of modules referencing forecast_regret changed. "
            "docs/reference-benchmark.md documents exactly who calls it, at "
            "which horizon, and why a field number may or may not be "
            "comparable to the benchmark's -- update that section, then "
            "update this test",
        )

        # test_the_doc_tells_a_reader_the_field_number_is_a_different_horizon: The specific thing a reader could get wrong: comparing
        doc = (self.repo / "docs" / "reference-benchmark.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("nowcast_skill", doc)
        self.assertIn("one-step-ahead", doc)


# --- test_the_package_never_shadows_stdlib_time -----------------------------------------------
# Loading the `time` platform sets the package's attribute `time` to time.py,
# which replaced a stdlib `import time` in __init__.py, and setup then failed
# on `time.monotonic()` (11 Oct 2026). A check that reads source text; the time
# entities themselves are in tests/v2/entities/test_switches_and_times.py
# (#1837).

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _helpers():
    src = (_PKG / "time.py").read_text(encoding="utf-8")
    start = src.index("def hours_to_time")
    end = src.index("async def async_setup_entry")
    ns: dict = {"time": time}
    exec(src[start:end], ns)  # noqa: S102 -- the pure helpers only, without HA imports
    return ns


def test_the_package_never_uses_the_name_time_for_the_stdlib():
    # test_the_package_never_uses_the_name_time_for_the_stdlib: Loading the `time` platform sets the package's attribute `time` to
    src = (_PKG / "__init__.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "\nimport time\n" not in "\n" + code + "\n"
    assert "time.monotonic(" not in code
    assert "time.time(" not in code

    # test_the_published_dict_carries_the_stamp
    keys = _published_literal_keys()
    assert "report_schema" in keys and "j_star_path_delta_explained" in keys

    # test_the_field_set_matches_the_schema_number
    digest = hashlib.sha256("\n".join(_published_literal_keys()).encode()).hexdigest()
    assert digest == _EXPECTED_HASHES.get(quality.QUALITY_REPORT_SCHEMA), (
        "The published quality-report field set changed. Bump "
        "QUALITY_REPORT_SCHEMA in solver_reports/quality.py and add "
        f"{{{quality.QUALITY_REPORT_SCHEMA + 1}: '{digest}'}} here."
    )

    # --- test_467_calendar_trip_windows -----------------------------------------------------------
    # #467: calendar trips are resolved into plain tuples, so the dependency points
    # one way: this module turns calendar data into numbers and the caller decides
    # which solver element to build. (Its Home Assistant boundary is enforced by
    # tests/v2/orchestration/test_calendar_trip_windows.py loading it by path with
    # no Home Assistant; an entity_id that is not Nimbus's own fails the #1994 gate.)
    # test_467_calendar_trips_imports_nothing_from_the_solver
    source = (_NIMBUS_LOAD / "solver_inputs" / "calendar_trips.py").read_text(
        encoding="utf-8"
    )
    assert "from ..solver" not in source
    assert "import AdequacyWindow" not in source


# --- test_1496_the_schema_number_tracks_the_fields --------------------------------------------
# nimbus #1496: the published quality report's literal field set is pinned to
# QUALITY_REPORT_SCHEMA, so adding a field without bumping it fails here
# instead of freezing older days at their old shape. A check that reads source,
# kept apart until the decision on source-reading tests (#1837); the rescore it
# drives is in tests/v2/reports/test_daily_reports.py.

quality = sys.modules[solver_writer.publish_daily_quality_report.__module__]


_EXPECTED_HASHES = {
    2: "0bddf26959893096070fcb8208c89ca885835b611bbaa6c7bee2e9817747149b",
    3: "11f0ea93fda61d94b545172f6035e39af40a9edf38e1cf3dd26e33fe4488a64d",
    4: "9ea656c1af6164288b86f941218fb1309d1fd0bf457f015c1b0c529ea64c522c",
}


def _published_literal_keys() -> list[str]:
    src = inspect.getsource(quality._compute_report_for_window)
    fn = ast.parse(src.lstrip()).body[0]
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return sorted(keys)
