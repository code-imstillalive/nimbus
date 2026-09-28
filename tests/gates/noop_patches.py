"""Gate: fails when a `patch`/`monkeypatch.setattr` site that was genuinely
exercised in the base commit goes silently unread in the head commit.

Spec: docs/specs/000-golden-master-and-gates.md, Part C --
`tests/gates/noop_patches.py`: "for each `patch`/`monkeypatch.setattr` site,
records whether the patched attribute is read during the test; fails when
one that was read is no longer read (the #1316 failure mode)."

## The #1316 failure mode this exists to catch

PR #1316 (`docs/architecture/tech-debt-plan.md` section 2, "The facade
criterion") names it precisely: a test does
`monkeypatch.setattr(solver_writer, "some_function", double)` (or the
`unittest.mock.patch.object` equivalent, which is what this codebase
overwhelmingly uses -- see "Why patch.object, not monkeypatch" below). If a
refactor moves `some_function`'s real implementation to a new module and
changes the CALLER to resolve it from there, the patch still lands
successfully on `solver_writer.some_function` -- it just isn't read by
anything any more. The test does not error. It passes, having silently
exercised the real function instead of the double. Green CI, zero coverage
of the path the test exists to check, and nothing in a normal test run says
so.

`tests/analyse_module_dependencies.py` (read before writing this, per the
task) names the same shape from the STATIC side -- its `--callers` mode
finds which names a `monkeypatch.setattr(module, ...)` targets, so a phase
can see up front which re-exports are "the sharp case." It does not, and by
its own docstring cannot, tell you whether a given test's patch actually got
exercised -- that requires running the test, which is what this tool adds.

## Why this is a genuinely hard problem, and what "solving" it means here

There is no general, safe way to observe "was attribute X, since replaced by
Y, read by arbitrary code" without either (a) executing the code and
watching what actually happens, or (b) full symbolic/dataflow analysis this
project has no infrastructure for and which would still miss anything
resolved dynamically. This tool takes approach (a): it runs the test suite
twice (once per commit, exactly like the golden master harness's own
"one fresh interpreter per scenario" pattern -- see `tests/golden/harness.py`
docstring for the precedent this follows), with a pytest plugin installed
that instruments `unittest.mock`/`pytest`'s own patching machinery to record
whether each patch site was actually touched during the test that created
it, then diffs base against head per (test, site).

**What "touched" means, precisely, and what it deliberately does not cover:**

- A patch whose replacement is a `Mock`/`MagicMock` (i.e. `patch(...)` or
  `patch.object(...)` called with no explicit `new=`/positional replacement
  -- this is ~98% of this codebase's own patch sites, measured by grepping
  for a literal callable third argument) is tracked by hooking
  `unittest.mock.CallableMixin.__call__` (was the mock ever CALLED) and
  `unittest.mock.NonCallableMock.__getattr__` (was any attribute of it ever
  READ -- covers e.g. `patch.object(module, "_LOGGER")` where the test cares
  about `_LOGGER.warning(...)`, a call on a CHILD mock one level down, not
  the patched object itself). Both hooks are installed exactly once, globally,
  for the whole pytest session -- they observe behaviour, they never change
  a mock's `call_count`, `mock_calls`, `return_value`, or anything a test's
  own `assert_called_*`/`assertEqual` checks; nothing about test semantics
  changes, only observation is added.
- A patch given an EXPLICIT non-Mock replacement (`patch.object(obj, "name",
  some_real_function)`, or `monkeypatch.setattr(obj, "name",
  some_real_function)`) is handled differently, because there is no Mock to
  hook: the replacement callable itself is swapped, before installation, for
  a thin wrapper that records a touch and then calls straight through to the
  original -- transparent to return value and exceptions. This trades away
  exactly one thing: a caller that does something with the REPLACEMENT
  ITSELF other than calling it (e.g. `issubclass(x, patched_name)`, or
  reading `patched_name.__name__` for a message) will see the wrapper
  instead of the original object. No occurrence of that shape exists in this
  codebase's current test suite (checked by hand against every
  `patch.object(..., <bare-name>)` site); a reviewer adding a new one should
  know this tool would misreport it as "touched" via the wrapper being
  looked at, without ever actually detecting non-call access.
- **A patch of a genuinely constant, non-callable value (a string, a number,
  a plain dict) is NOT tracked at all** -- there is no Mock and no callable
  to wrap, so a plain attribute read (`if solver_writer.PLAN_STATE_PATH ==
  ...`) cannot be observed without instrumenting the container's own
  `__getattribute__`, which is invasive enough (it would have to intercept
  EVERY attribute access on `solver_writer`, a module imported by roughly
  the whole test suite) that the risk of a false failure or a real slowdown
  outweighs the coverage gained for what is, by construction, also the one
  case #1316's own failure mode cannot occur for in the shape that motivated
  this tool: #1316 is about a CALL silently missing its target, and a
  constant is never "called" by the caller either way. `--report-untracked`
  lists every constant-value patch site found statically so a reviewer can
  see what is out of scope rather than assume full coverage.
- `unittest.mock.patch.dict` is used in this codebase (11 sites, all faking
  `os.environ`) but is not instrumented: it returns a `_patch_dict`, a
  different class from `_patch` entirely, so it never goes through the
  `_patch.__enter__` hook this tool installs. This is a real gap, not a
  deliberate simplification for its own sake -- but `patch.dict` also isn't
  the #1316 shape: it temporarily mutates a dict's CONTENTS in place rather
  than swapping out a name a caller resolves at call time, so a code-move
  refactor cannot make it a silent no-op the way #1316 describes (`os.
  environ` itself doesn't move). Left untracked and named here rather than
  silently missing.
- `monkeypatch.setattr` is handled for its dominant real form in this
  codebase, `monkeypatch.setattr(obj, "name", value)` (three positional
  arguments) -- the ONE real usage found by grep across the whole suite
  uses exactly this form. The two-argument dotted-string form
  (`monkeypatch.setattr("module.path.attr", value)`) is detected and a
  best-effort site key is derived, but if pytest's own import-path
  resolution for that form ever disagrees with this tool's, the site is
  silently left untracked rather than risk misattributing it -- fails open,
  never fails the run for a reason unrelated to the actual regression being
  checked for.

**Matching a site across base and head** is by `(test nodeid, target
descriptor)`, not by file/line -- a refactor moves code, and line numbers
inside a spec's own retargeted patch calls are expected to shift. The target
descriptor is derived from the patcher's own resolved target and attribute
name (e.g. `"solver_writer.ha_get"`), not from the source text of the
`patch(...)` call, so a spec that legitimately RETARGETS a patch path (the
plan's own allowance, section 2's "facade criterion") produces a NEW
descriptor rather than colliding with the old one -- which is correct: that
old site is gone in head (nothing to regress) and the new one has no base
counterpart to compare against (nothing to regress from either), so a
retargeted patch is invisible to this gate by design, exactly like
`assertions_unchanged.py`'s own stated blind spot for a renamed test
function. A site that keeps the SAME descriptor across base and head and
flips from touched to untouched is precisely the case with nothing
legitimate to explain it: the target didn't move, but reading it stopped.

## Why `patch`/`patch.object`, not `monkeypatch.setattr`, dominates here

Measured by grep across `tests/`: 583 `patch.object(` sites, 105 `patch(`
sites, 1 real `monkeypatch.setattr(` site (`tests/hass_integration/
test_flap_regression_state_stability.py`). Spec 000's own table (`docs/
architecture/tech-debt-plan.md`) gives "512 patch / monkeypatch.setattr
sites, across 80 test files" as one combined figure; this grep is consistent
with nearly all of them being `unittest.mock.patch`/`patch.object`. The
`monkeypatch.setattr` path in this tool is real and tested (see
`tests/test_gates_noop_patches.py`) but lightly exercised against the real
suite for exactly this reason.

## Practical scope: which tests actually run

Only test files that STATICALLY contain a `patch(`, `patch.object(`, or
`monkeypatch.setattr(` call (found by the same AST walk used for
`--report-untracked`) are selected to run under instrumentation by default.
A test file with no patch site cannot regress this gate -- there is nothing
in it to compare -- so running it under instrumentation would only cost
time for zero additional information. `--tests` overrides this with an
explicit pytest selector (file, directory, or node id) when a narrower or
wider run is wanted.

## Running each commit

Exactly like `assertions_unchanged.py`: `git worktree add --detach` (or
`--base-dir`/`--head-dir` to skip it) checks out `--base`/`--head`, and each
one's selected tests run in a FRESH subprocess (`python -m pytest`) with this
same file loaded as a pytest plugin via `-p noop_patches` (its plugin hooks
are always defined at module scope; the CLI/driver logic below only runs
under `if __name__ == "__main__"`, so importing this file as a plugin never
tries to re-run the comparison). `NOOP_PATCH_TRACK_OUTPUT` tells the child
where to write its JSON record of `{test nodeid: {site descriptor: touched
bool}}`; the parent process reads both records back and diffs them.
"""

from __future__ import annotations

import argparse
import ast
import functools
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_THIS_FILE = Path(__file__).resolve()
_THIS_DIR = _THIS_FILE.parent

# ---------------------------------------------------------------------------
# Part 1: the pytest plugin. Module-scope hook functions so `-p noop_patches`
# (this file's own module name) registers them purely by being imported --
# no separate plugin file needed, and no risk of the CLI code below running
# just because pytest imported this module.
# ---------------------------------------------------------------------------

_TRACK_OUTPUT = os.environ.get("NOOP_PATCH_TRACK_OUTPUT")

if _TRACK_OUTPUT:
    import unittest.mock as _mock

    import pytest

    _records: dict[str, dict[str, bool]] = {}
    _current_test: list[str] = []
    _tracked_mock_ids: dict[int, str] = {}
    _untracked_constant_sites: dict[str, list[str]] = {}

    def _mark_touched(site_key: str) -> None:
        if not _current_test:
            return
        _records.setdefault(_current_test[-1], {})[site_key] = True

    def _mark_established(site_key: str) -> None:
        if not _current_test:
            return
        _records.setdefault(_current_test[-1], {}).setdefault(site_key, False)

    def _describe_target(patcher: _mock._patch) -> str:
        try:
            target = patcher.getter()
            target_name = getattr(target, "__name__", None) or getattr(
                target, "__qualname__", None
            )
            if target_name is None:
                target_name = repr(target)
            attribute = getattr(patcher, "attribute", "?")
            return f"{target_name}.{attribute}"
        except Exception:  # noqa: BLE001 -- introspection is best-effort; any
            # failure here must never break the test run this plugin is
            # instrumenting, only degrade the site's own descriptor.
            return f"<unresolvable:{patcher!r}>"

    def _wrap_callable(fn, site_key: str):
        if getattr(fn, "_noop_patch_tracked_site", None) == site_key:
            return fn

        @functools.wraps(fn)
        def _tracking_wrapper(*args, **kwargs):
            _mark_touched(site_key)
            return fn(*args, **kwargs)

        _tracking_wrapper._noop_patch_tracked_site = site_key
        return _tracking_wrapper

    _original_patch_enter = _mock._patch.__enter__

    def _tracking_patch_enter(self):
        site_key = _describe_target(self)
        is_default_new = self.new is _mock.DEFAULT
        if (
            not is_default_new
            and callable(self.new)
            and not isinstance(self.new, _mock.NonCallableMock)
        ):
            # An explicit, non-Mock replacement (e.g. `patch.object(obj,
            # "name", real_function)`) -- wrap it before installation so a
            # call to it is observable. See the module docstring's "What
            # touched means" section for what this trades away.
            self.new = _wrap_callable(self.new, site_key)
        result = _original_patch_enter(self)
        if isinstance(result, _mock.NonCallableMock):
            # The common shape: `patch(...)`/`patch.object(...)` with no
            # explicit replacement auto-creates a MagicMock. Tracked via the
            # global CallableMixin/`NonCallableMock.__getattr__` hooks below.
            _tracked_mock_ids[id(result)] = site_key
            _mark_established(site_key)
        elif not is_default_new and callable(self.new):
            # `result` here IS `self.new` (already wrapped above), by
            # `_patch.__enter__`'s own contract of returning `new` verbatim
            # when it isn't DEFAULT. Register the site even if it turns out
            # never to be called, so a comparison has something to diff.
            _mark_established(site_key)
        else:
            # A real constant (non-callable, non-Mock) replacement. Cannot
            # be tracked -- see the module docstring. Recorded so
            # `--report-untracked` can say so honestly instead of staying silent.
            _untracked_constant_sites.setdefault(
                _current_test[-1] if _current_test else "<unknown>", []
            ).append(site_key)
        if isinstance(result, _mock.NonCallableMock):
            self._noop_patch_tracked_id = id(result)
        return result

    _mock._patch.__enter__ = _tracking_patch_enter  # type: ignore[method-assign]

    _original_patch_exit = _mock._patch.__exit__

    def _tracking_patch_exit(self, *exc_info):
        # Real, observed necessity, not defensive-programming for its own
        # sake: CPython reuses a freed object's memory address for the next
        # allocation, and this suite runs 700+ tests (so thousands of
        # `patch`/`patch.object` mocks) in ONE process. Without popping the
        # id->site mapping here, a later, wholly unrelated test's freshly
        # created mock can land at the same id() as an earlier, already-
        # exited mock, and get wrongly credited with that earlier site's
        # touch. Caught exactly this way: comparing this repo's own real
        # `tests/` tree against itself (base == head, see the PR body)
        # produced one single-site false "regression" that vanished when
        # that same site was run in isolation -- the site was never touched
        # in either run, and the full-suite run's apparent "True" on base
        # was a stale id() collision from a different test's mock, live
        # proof this cleanup is required and not merely theoretical.
        tracked_id = getattr(self, "_noop_patch_tracked_id", None)
        if tracked_id is not None:
            _tracked_mock_ids.pop(tracked_id, None)
        return _original_patch_exit(self, *exc_info)

    _mock._patch.__exit__ = _tracking_patch_exit  # type: ignore[method-assign]

    _original_call = _mock.CallableMixin.__call__

    def _tracking_call(self, *args, **kwargs):
        site_key = _tracked_mock_ids.get(id(self))
        if site_key is not None:
            _mark_touched(site_key)
        return _original_call(self, *args, **kwargs)

    _mock.CallableMixin.__call__ = _tracking_call  # type: ignore[method-assign]

    _original_getattr = _mock.NonCallableMock.__getattr__

    def _tracking_getattr(self, name):
        if not name.startswith("_"):
            site_key = _tracked_mock_ids.get(id(self))
            if site_key is not None:
                _mark_touched(site_key)
        return _original_getattr(self, name)

    _mock.NonCallableMock.__getattr__ = _tracking_getattr  # type: ignore[method-assign]

    try:
        from _pytest.monkeypatch import NOTSET, MonkeyPatch

        _original_mp_setattr = MonkeyPatch.setattr

        def _tracking_mp_setattr(self, target, name, value=NOTSET, raising=True):
            """Wraps pytest's own `MonkeyPatch.setattr`, which has two real
            call shapes: `setattr(obj, "name", value)` (three positionals,
            the dominant form in this codebase) and `setattr("mod.attr",
            value)` (two positionals -- `value` stays NOTSET and the real
            replacement arrives as `name`). Only intercepts a callable,
            non-Mock replacement (see the module docstring); anything else
            is handed straight to the real implementation unmodified.
            """
            wrapped_name, wrapped_value = name, value
            try:
                if (
                    value is not NOTSET
                    and not isinstance(target, str)
                    and isinstance(name, str)
                    and callable(value)
                    and not isinstance(value, _mock.NonCallableMock)
                ):
                    site_key = f"{getattr(target, '__name__', repr(target))}.{name}"
                    wrapped_value = _wrap_callable(value, site_key)
                    _mark_established(site_key)
                elif (
                    value is NOTSET
                    and isinstance(target, str)
                    and callable(name)
                    and not isinstance(name, _mock.NonCallableMock)
                ):
                    module_path, _, attr = target.rpartition(".")
                    if module_path:
                        site_key = f"{module_path}.{attr}"
                        wrapped_name = _wrap_callable(name, site_key)
                        _mark_established(site_key)
            except Exception:  # noqa: BLE001 -- best-effort site detection;
                # any failure here must fall open to the real, unwrapped
                # setattr call rather than break the test it's observing.
                wrapped_name, wrapped_value = name, value
            return _original_mp_setattr(
                self, target, wrapped_name, wrapped_value, raising=raising
            )

        MonkeyPatch.setattr = _tracking_mp_setattr  # type: ignore[method-assign]
    except ImportError:  # pragma: no cover - pytest always provides this
        pass

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(item, nextitem):
        """Spans setup + call + teardown, so a `setUp()`-installed patcher
        (`self.patcher = patch(...); self.patcher.start()`) is attributed to
        the right test, not silently dropped because it ran outside the
        'call' phase."""
        _current_test.append(item.nodeid)
        try:
            yield
        finally:
            _current_test.pop()

    def pytest_sessionfinish(session, exitstatus):
        Path(_TRACK_OUTPUT).write_text(
            json.dumps(
                {"records": _records, "untracked_constants": _untracked_constant_sites},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Part 2: static scan, used to (a) pick which files to run by default and
# (b) report constant-value patch sites this tool cannot track (honesty,
# not detection).
# ---------------------------------------------------------------------------


def _file_has_patch_sites(source: str) -> bool:
    return (
        ("patch(" in source)
        or ("patch.object(" in source)
        or ("monkeypatch.setattr(" in source)
    )


def _list_candidate_test_files(root: Path) -> list[Path]:
    """Every `tests/test_*.py` with a static patch/monkeypatch site.

    `tests/hass_integration/` is excluded, matching CI's own split
    (`--ignore=tests/hass_integration/` on the stub-based invocation this
    tool's subprocess call mirrors -- see `_run_tracked`). That directory
    needs the real `pytest-homeassistant-custom-component` harness (a
    genuine `hass` fixture, a real event loop), which this tool does not
    drive; its one real `monkeypatch.setattr` site (grepped, confirmed:
    `tests/hass_integration/test_flap_regression_state_stability.py`) is
    out of scope for the same reason the stub-based CI job leaves the whole
    directory out. `--tests` can still target it explicitly.
    """
    tests_dir = root / "tests"
    out = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        if "hass_integration" in path.relative_to(tests_dir).parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _file_has_patch_sites(source):
            out.append(path)
    return out


def _static_patch_site_count(root: Path) -> int:
    """Companion metric only -- not used for the pass/fail decision."""
    total = 0
    for path in _list_candidate_test_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("object",) and isinstance(
                    node.func.value, ast.Name
                ):
                    if node.func.value.id == "patch":
                        total += 1
                elif node.func.attr == "setattr" and _looks_like_monkeypatch(node.func):
                    total += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "patch"
            ):
                total += 1
    return total


def _looks_like_monkeypatch(func: ast.Attribute) -> bool:
    value = func.value
    return isinstance(value, ast.Name) and "monkeypatch" in value.id.lower()


# ---------------------------------------------------------------------------
# Part 3: the driver -- runs base and head, each in its own subprocess/
# interpreter, and diffs the two records.
# ---------------------------------------------------------------------------


@dataclass
class Regression:
    test_id: str
    site: str

    def describe(self) -> str:
        return f"{self.test_id} :: {self.site}"


@dataclass
class Report:
    regressions: list[Regression] = field(default_factory=list)
    base_sites: int = 0
    head_sites: int = 0
    tests_run_base: int = 0
    tests_run_head: int = 0
    untracked_constants_head: dict[str, list[str]] = field(default_factory=dict)
    # Set when the comparison could not have found anything, whatever the
    # code under test did. A gate that measured nothing has not passed --
    # see `compare_runs`.
    vacuous_reason: str | None = None

    @property
    def failed(self) -> bool:
        return bool(self.regressions) or self.vacuous_reason is not None


# pytest's own documented exit codes. 0 (all passed) and 1 (some tests
# failed) both mean the run really happened, which is all this tool needs --
# it cares whether a patch site was READ, not whether the test passed. The
# rest mean the run did not happen as asked: 2 interrupted (a collection
# error -- a missing plugin or dependency in the tree being measured),
# 3 internal error, 4 bad usage, 5 nothing collected. Every one of those
# yields an EMPTY record, which older versions of this tool then compared
# against another empty record and reported as OK.
_PYTEST_RC_RAN = frozenset({0, 1})


def _run_tracked(root: Path, tests_selector: list[str] | None) -> dict:
    files = tests_selector or [
        str(p.relative_to(root)) for p in _list_candidate_test_files(root)
    ]
    if not files:
        # No candidate files is itself a non-measurement, not a pass. Report
        # it as a distinct rc so the caller can say which of the two trees
        # produced nothing, rather than silently diffing {} against {}.
        return {"records": {}, "untracked_constants": {}, "pytest_returncode": 5}
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as out_file:
        output_path = out_file.name
    env = dict(os.environ)
    env["NOOP_PATCH_TRACK_OUTPUT"] = output_path
    env["PYTHONPATH"] = os.pathsep.join([str(_THIS_DIR), env.get("PYTHONPATH", "")])
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *files,
        "-p",
        "noop_patches",
        "-p",
        "no:cacheprovider",
        # Same reason CI's own stub-based job disables this (see CLAUDE.md,
        # "Testing"): registering the real pytest-homeassistant-custom-
        # component plugin breaks the stub-based suite two independent
        # ways. This tool's default file selection never includes
        # tests/hass_integration/, so the real-HA-harness plugin brings no
        # benefit here, only that breakage.
        "-p",
        "no:homeassistant",
        "-q",
        "--no-header",
    ]
    # check=False: a real test FAILURE inside the tracked run is an
    # ordinary, expected outcome here (this tool cares whether a patch was
    # touched, not whether the test passed) -- the JSON output is read
    # regardless of pytest's own exit code.
    result = subprocess.run(
        cmd, cwd=root, env=env, capture_output=True, text=True, check=False
    )
    try:
        data = json.loads(Path(output_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"records": {}, "untracked_constants": {}}
        print(
            "warning: no tracking output produced; pytest output follows:",
            file=sys.stderr,
        )
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
    finally:
        Path(output_path).unlink(missing_ok=True)
    data["pytest_returncode"] = result.returncode
    if result.returncode not in _PYTEST_RC_RAN:
        # Surface it here rather than only in the exit code: a collection
        # error prints the real cause (an ImportError naming the missing
        # module) and that is what the person reading a red gate needs.
        print(
            f"noop_patches: pytest exited {result.returncode} in {root} -- the "
            "tracked run did not happen. Output follows:",
            file=sys.stderr,
        )
        print(result.stdout[-4000:], file=sys.stderr)
        print(result.stderr[-4000:], file=sys.stderr)
    return data


def compare_runs(base_data: dict, head_data: dict) -> Report:
    """Diff two tracked runs.

    A comparison that measured NOTHING is a failure, not a pass. This is the
    #1354 shape, fixed there for `coverage_compare.py` ("reports PASS when it
    measured 0 lines") and present here until it was reproduced on real
    commits: running this tool with `--base`/`--head` and a `--tests`
    selector whose file could not be collected in the child (a missing pytest
    plugin, in the real case) printed `base ran 0 test(s)` and then
    `OK: no patch site ... went unread`. Every gate in this repo has now hit
    some version of the same lesson: a check that cannot fail is
    indistinguishable from a check that works.
    """
    report = Report()
    base_records = base_data.get("records", {})
    head_records = head_data.get("records", {})
    report.tests_run_base = len(base_records)
    report.tests_run_head = len(head_records)
    report.untracked_constants_head = head_data.get("untracked_constants", {})

    base_rc = base_data.get("pytest_returncode")
    head_rc = head_data.get("pytest_returncode")
    if base_rc is not None and base_rc not in _PYTEST_RC_RAN:
        report.vacuous_reason = (
            f"the base tree's tracked pytest run exited {base_rc}, so nothing "
            "was measured there (see its output above)"
        )
    elif head_rc is not None and head_rc not in _PYTEST_RC_RAN:
        report.vacuous_reason = (
            f"the head tree's tracked pytest run exited {head_rc}, so nothing "
            "was measured there (see its output above)"
        )
    elif not base_records:
        report.vacuous_reason = (
            "the base tree recorded no tests at all -- nothing could have been compared"
        )
    for test_id, base_sites in base_records.items():
        head_sites = head_records.get(test_id)
        if head_sites is None:
            continue  # test removed/renamed -- out of scope, see docstring
        for site, was_touched in base_sites.items():
            report.base_sites += 1
            if not was_touched:
                continue  # not exercised on base either -- nothing to regress
            now_touched = head_sites.get(site)
            if now_touched is None:
                continue  # site retargeted/removed in head -- out of scope
            if not now_touched:
                report.regressions.append(Regression(test_id, site))
    for test_id, head_sites in head_records.items():
        report.head_sites += len(head_sites)
    if report.vacuous_reason is None and report.base_sites == 0:
        # Tests ran on base, but not one of them established a patch site
        # this tool can track. There is nothing for a head run to regress
        # FROM, so a clean result here says nothing about the change --
        # the selector is wrong, or every site in it is an untrackable
        # constant (see the module docstring).
        report.vacuous_reason = (
            f"the base tree ran {report.tests_run_base} test(s) but recorded 0 "
            "trackable patch site(s) -- the selection contains nothing this "
            "gate can check"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument(
        "--tests",
        nargs="*",
        default=None,
        help="explicit pytest selector(s), relative to the repo root, overriding "
        "the default (every tests/test_*.py file containing a static patch site)",
    )
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--head-dir", default=None)
    parser.add_argument(
        "--report-untracked",
        action="store_true",
        help="also list constant-value patch sites this tool cannot track (honesty output)",
    )
    args = parser.parse_args(argv)

    if args.base_dir and args.head_dir:
        base_root, head_root = Path(args.base_dir), Path(args.head_dir)
        base_data = _run_tracked(base_root, args.tests)
        head_data = _run_tracked(head_root, args.tests)
        report = compare_runs(base_data, head_data)
        _print_report(report, args)
        return 1 if report.failed else 0

    with tempfile.TemporaryDirectory(prefix="noop_patches_") as tmp:
        tmp_root = Path(tmp)
        base_wt = _worktree(args.base, tmp_root, "base")
        head_wt = _worktree(args.head, tmp_root, "head")
        try:
            base_data = _run_tracked(base_wt, args.tests)
            head_data = _run_tracked(head_wt, args.tests)
        finally:
            _remove_worktree(base_wt)
            _remove_worktree(head_wt)
    report = compare_runs(base_data, head_data)
    _print_report(report, args)
    return 1 if report.failed else 0


def _worktree(ref: str, tmp_root: Path, label: str) -> Path:
    dest = tmp_root / label
    subprocess.run(
        ["git", "worktree", "add", "--detach", "--quiet", str(dest), ref],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


def _remove_worktree(path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _print_report(report: Report, args: argparse.Namespace) -> None:
    print(
        f"noop_patches: base ran {report.tests_run_base} test(s) with "
        f"{report.base_sites} live patch site(s) checked; head ran "
        f"{report.tests_run_head} test(s), {report.head_sites} site(s) recorded"
    )
    if args.report_untracked and report.untracked_constants_head:
        total = sum(len(v) for v in report.untracked_constants_head.values())
        print(
            f"  {total} constant-value patch site(s) not trackable (see module docstring):"
        )
        for test_id, sites in sorted(report.untracked_constants_head.items()):
            for site in sites:
                print(f"    {test_id} :: {site}")
    if report.vacuous_reason is not None:
        print(f"FAIL (measured nothing): {report.vacuous_reason}.")
        print(
            "  A gate that measured nothing has not passed. Fix the selection "
            "or the tracked run before reading this result as clean."
        )
        return
    if not report.regressions:
        print("OK: no patch site that was read on base went unread on head.")
        return
    print(
        f"FAIL: {len(report.regressions)} patch site(s) went from touched to untouched:\n"
    )
    for regression in report.regressions:
        print(f"  {regression.describe()}")


if __name__ == "__main__":
    raise SystemExit(main())
