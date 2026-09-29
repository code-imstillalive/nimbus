"""Proves `tests/gates/coverage_compare.py` on a deliberately bad synthetic
case, per docs/specs/000-golden-master-and-gates.md Part C and the tech-debt
plan's "Part C tools, one PR each, each proven on a deliberately bad commit."

Synthetic git repos only for the bad/good pairs -- two commits each, driven
through `--exercise-command` (this tool's own override for exactly this
purpose) instead of the real nimbus golden harness and test suite, so this
file is fast and does not depend on `custom_components/nimbus_load` at all.
One real check at the end runs the actual default pipeline (golden scenarios
+ full suite) with `--base HEAD --head HEAD` against this real checkout, to
prove the tool comes back clean comparing `main` to itself.

**Run this file with `-p no:homeassistant`** (`pytest
tests/test_gates_coverage_compare.py -p no:homeassistant`), same as
`test_main_golden_output_guardrail.py` -- without it, the `homeassistant`
pytest11 plugin loads for every invocation in the process (it is registered
via entry_points, not opted into per-file) and its event loop opens a
self-pipe socket, which `pytest_socket` blocks with
`SocketBlockedError` during fixture setup. CI's stub-suite job already
passes this flag; it is only a trap when this file is run by hand. See
docs/specs/000-golden-master-and-gates.md's "Found while building it"
section for this and the nimbus #1354 fixes (a vacuous PASS on zero
measured base coverage, and the Windows short-path aliasing that caused it
here).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent / "gates" / "coverage_compare.py"
REPO = Path(__file__).resolve().parents[1]


def _worktree_paths(repo: Path) -> set[str]:
    """Every path currently registered as a worktree of `repo`, per `git
    worktree list --porcelain`'s own `worktree <path>` lines."""
    proc = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        line[len("worktree ") :]
        for line in proc.stdout.splitlines()
        if line.startswith("worktree ")
    }


@pytest.fixture(autouse=True)
def _cleanup_leaked_coverage_compare_worktrees() -> Iterator[None]:
    """Backstop for nimbus #1354's own leak finding: after one afternoon of
    runs against this real checkout, `git worktree list` showed 7 stale
    `coverage-compare-*/wt-{base,head}` registrations. `coverage_compare.py`
    itself now wraps both worktrees in one try/finally (see its own `run()`),
    which covers a Python-level exception -- but a hard timeout
    (`subprocess.run(..., timeout=...)` calling `process.kill()`, or any
    other forced termination) can still skip that `finally` entirely, since
    `TerminateProcess`/`SIGKILL` do not run Python cleanup code. This fixture
    is the independent, environment-level guard: snapshot this repo's own
    worktree registrations before the test, and force-remove any new
    `coverage-compare-` one still present after -- regardless of why the
    test itself passed, failed, or timed out. Scoped to `REPO` (this real
    checkout) because that is the only repo any test in this file leaks
    against; the toy-repo tests register worktrees against their own
    throwaway `toy_repo/.git`, which pytest's `tmp_path` cleans up on its
    own schedule and never accumulates inside this real repo either way.
    """
    before = _worktree_paths(REPO)
    yield
    after = _worktree_paths(REPO)
    for path in after - before:
        if "coverage-compare-" not in path:
            continue
        subprocess.run(
            ["git", "worktree", "remove", "--force", path],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(root: Path) -> None:
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "gate-test@example.invalid"],
        ["git", "config", "user.name", "Gate Test"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True, text=True)


def _commit(root: Path, message: str) -> None:
    subprocess.run(
        ["git", "add", "-A"], cwd=root, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def toy_repo(tmp_path: Path) -> Path:
    root = tmp_path / "toy"
    (root / "pkg").mkdir(parents=True)
    _init_repo(root)
    return root


# ---------------------------------------------------------------------------
# nimbus #1354, finding 2: unit test for `canonical_worktree_root` directly,
# rather than only end-to-end through a real short-path-aliased filesystem.
# On a Windows box whose temp directory is actually 8.3-short-path-aliased
# (confirmed live while fixing #1354: `tempfile.gettempdir()` returned
# `C:\Users\RAF_LO~1\AppData\Local\Temp` there), the toy-repo tests above
# already exercise this end-to-end -- that IS the mechanism that made them
# fail before this fix (0 lines covered at base, on every one of them). But
# that reproduction only fires on a filesystem with a short-path alias to
# find, which most CI runners and non-Windows machines never have. This
# test proves the helper's own logic -- "resolve through the same
# canonicalisation coverage.py uses" -- without depending on the host
# filesystem having one, by substituting a short-to-long mapping for
# `os.path.realpath` itself.
# ---------------------------------------------------------------------------


def test_canonical_worktree_root_resolves_short_paths(
    monkeypatch, tmp_path: Path
) -> None:
    sys.path.insert(0, str(GATE.parent))
    import coverage_compare

    short = tmp_path / "RAF_LO~1" / "Temp"
    long = tmp_path / "Raf_local" / "Temp"

    def fake_realpath(path: object) -> str:
        # Mirrors the real bug: any path under the short alias resolves to
        # the long form, everything else resolves to itself unchanged --
        # exactly what os.path.realpath does for an 8.3-aliased directory
        # on Windows.
        text = str(path)
        if text == str(short):
            return str(long)
        return text

    monkeypatch.setattr(coverage_compare.os.path, "realpath", fake_realpath)

    assert coverage_compare.canonical_worktree_root(short) == long
    # A path with no aliasing to resolve is returned unchanged -- the
    # helper must not invent a difference where none exists.
    assert coverage_compare.canonical_worktree_root(long) == long


# The gate resolves worktrees against its own module-level `REPO` constant.
# A subprocess can't have that patched from outside, so route every call
# through a tiny runner that imports the real module, points `REPO` at the
# toy repo, and calls `main()` directly -- the same pattern
# test_gates_size_ratchet.py uses for its own ratchet-mode tests.
_RUNNER = f"""
import sys
sys.path.insert(0, {str(GATE.parent)!r})
import coverage_compare

repo, args = sys.argv[1], sys.argv[2:]
coverage_compare.REPO = __import__("pathlib").Path(repo)
sys.exit(coverage_compare.main(args))
"""


def _run_gate(root: Path, *args: str) -> subprocess.CompletedProcess:
    # `--no-reap` on every call (nimbus #1405). `main()` otherwise reaps stale
    # worktrees and temp trees from the REAL system temp dir, and these tests
    # are about the comparison logic, not the reaper -- a test that deletes a
    # developer's files as a side effect is the same flaw
    # test_1405_reap_stale_worktrees.py had to fix in itself. The reaper has its
    # own hermetic tests; this funnel opts out.
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, str(root), "--no-reap", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


_COVERED_FN = "def covered_fn():\n    a = 1\n    b = 2\n    return a + b\n"


def _write_exercise(root: Path, calls: list[str]) -> None:
    lines = ["import sys", "sys.path.insert(0, 'pkg')", "import mod"]
    lines += calls
    (root / "exercise.py").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Bad commit: a code path that ran at base is dropped and exercised nowhere.
# ---------------------------------------------------------------------------


def test_fails_when_a_covered_code_path_is_dropped(toy_repo: Path) -> None:
    mod = toy_repo / "pkg" / "mod.py"
    mod.write_text(
        _COVERED_FN
        + "\n\ndef dropped_fn():\n    x = 10\n    y = 20\n    return x + y\n"
    )
    _write_exercise(toy_repo, ["print(mod.covered_fn())", "print(mod.dropped_fn())"])
    _commit(toy_repo, "base: dropped_fn is exercised")

    mod.write_text(_COVERED_FN)  # dropped_fn removed outright, moved nowhere
    _write_exercise(toy_repo, ["print(mod.covered_fn())"])
    _commit(toy_repo, "head: dropped_fn removed, no longer exercised")

    proc = _run_gate(
        toy_repo,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--source-file",
        "pkg/mod.py",
        "--exercise-command",
        "exercise.py",
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL" in proc.stdout
    assert "dropped_fn" in proc.stdout
    assert "x = 10" in proc.stdout


# ---------------------------------------------------------------------------
# Good commit: the same code, verbatim, relocated to a new file and still
# exercised there -- the tech-debt plan's own definition of a compliant move.
# ---------------------------------------------------------------------------


def test_passes_when_a_covered_function_moves_to_a_declared_destination(
    toy_repo: Path,
) -> None:
    mod = toy_repo / "pkg" / "mod.py"
    mod.write_text(
        _COVERED_FN + "\n\ndef moved_fn():\n    x = 10\n    y = 20\n    return x + y\n"
    )
    _write_exercise(toy_repo, ["print(mod.covered_fn())", "print(mod.moved_fn())"])
    _commit(toy_repo, "base: moved_fn lives in mod.py")

    mod.write_text(_COVERED_FN)
    extracted = toy_repo / "pkg" / "extracted.py"
    extracted.write_text("def moved_fn():\n    x = 10\n    y = 20\n    return x + y\n")
    _write_exercise(
        toy_repo,
        ["import extracted", "print(mod.covered_fn())", "print(extracted.moved_fn())"],
    )
    _commit(toy_repo, "head: moved_fn relocated to extracted.py, unchanged")

    moved_code = toy_repo / "moved.json"
    moved_code.write_text(json.dumps({"moved_to": ["pkg/extracted.py"]}))

    common = [
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--source-file",
        "pkg/mod.py",
        "--exercise-command",
        "exercise.py",
    ]

    # Without the mapping, the moved lines have nowhere to be found -- fails,
    # proving the mapping (not some accidental leniency) is what makes the
    # second call pass.
    without_mapping = _run_gate(toy_repo, *common)
    assert without_mapping.returncode == 1, (
        without_mapping.stdout + without_mapping.stderr
    )

    with_mapping = _run_gate(toy_repo, *common, "--moved-code", str(moved_code))
    assert with_mapping.returncode == 0, with_mapping.stdout + with_mapping.stderr
    assert "PASS" in with_mapping.stdout
    assert "extracted.py" in with_mapping.stdout


def test_passes_comparing_a_ref_to_itself(toy_repo: Path) -> None:
    mod = toy_repo / "pkg" / "mod.py"
    mod.write_text(_COVERED_FN)
    _write_exercise(toy_repo, ["print(mod.covered_fn())"])
    _commit(toy_repo, "one commit")

    proc = _run_gate(
        toy_repo,
        "--base",
        "HEAD",
        "--head",
        "HEAD",
        "--source-file",
        "pkg/mod.py",
        "--exercise-command",
        "exercise.py",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout


def test_reports_an_unreachable_never_called_function_as_uncovered_both_sides(
    toy_repo: Path,
) -> None:
    """A function nobody calls contributes nothing to either side's covered
    set -- it must not appear as a false regression just because it exists
    in the source."""
    mod = toy_repo / "pkg" / "mod.py"
    mod.write_text(_COVERED_FN + "\n\ndef never_called():\n    return None\n")
    _write_exercise(toy_repo, ["print(mod.covered_fn())"])
    _commit(toy_repo, "base")
    (toy_repo / "pkg" / "mod.py").write_text(
        _COVERED_FN + "\n\ndef never_called():\n    return None\n  # unchanged\n"
    )
    _commit(toy_repo, "head: trivial comment touch")

    proc = _run_gate(
        toy_repo,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--source-file",
        "pkg/mod.py",
        "--exercise-command",
        "exercise.py",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# nimbus #1354, finding 1: a base side that measured ZERO covered lines must
# not read as PASS. Reproduced directly (no path-aliasing trick needed):
# pkg/mod.py exists at both commits but the exercise script never imports
# it, so its covered-line count is 0 at base by construction -- the same
# measurement gap the real Windows short-path bug (finding 2, unit-tested
# above via test_canonical_worktree_root_resolves_short_paths, and exercised
# end-to-end by the toy-repo tests further below on a genuinely
# short-path-aliased filesystem) produced by accident.
# ---------------------------------------------------------------------------


def test_fails_when_base_side_measured_zero_covered_lines(toy_repo: Path) -> None:
    """ "Every line covered at base is covered at head" holds vacuously over
    an empty set -- before this fix, a base side that measured nothing was
    reported identically to one that measured everything and found no
    regression: same "PASS" text, same exit code 0. `run()` must now refuse
    PASS and return a third, distinct exit code (`NO_BASE_COVERAGE_EXIT_CODE`
    in coverage_compare.py) so a CI caller can tell "nothing was measured"
    apart from both "measured everything, no regression" (0) and "measured
    a real regression" (1)."""
    mod = toy_repo / "pkg" / "mod.py"
    mod.write_text(_COVERED_FN)
    (toy_repo / "exercise.py").write_text("print('pkg/mod.py is never imported')\n")
    _commit(toy_repo, "base: mod.py exists but nothing imports it")

    (toy_repo / "exercise.py").write_text(
        "print('pkg/mod.py is never imported')\n# head: trivial touch\n"
    )
    _commit(toy_repo, "head: still never imported")

    proc = _run_gate(
        toy_repo,
        "--base",
        "HEAD~1",
        "--head",
        "HEAD",
        "--source-file",
        "pkg/mod.py",
        "--exercise-command",
        "exercise.py",
    )
    assert proc.returncode not in (0, 1), proc.stdout + proc.stderr
    assert proc.returncode == 2, proc.stdout + proc.stderr  # NO_BASE_COVERAGE_EXIT_CODE
    # The real PASS status line ("PASS: every line covered...") must never
    # appear -- checked as that specific marker, not a bare "PASS" substring,
    # since the refusal message itself legitimately says "refusing to report
    # PASS" as part of explaining why it did not pass.
    assert "PASS: every line covered" not in proc.stdout
    assert "0 lines covered at base" in proc.stdout
    assert "refusing to report PASS" in proc.stdout


# ---------------------------------------------------------------------------
# One real check against the actual repo: `main` compared to itself must
# come back clean under the tool's real default pipeline (golden scenarios
# plus the full stub suite, not --exercise-command).
# ---------------------------------------------------------------------------


def test_real_main_compared_to_itself_has_zero_regressions() -> None:
    # No custom pytest marker: this repo runs with --strict-markers and
    # registers none (confirmed by grepping pyproject.toml and every
    # existing test file before adding one here). This test is inherently
    # slower than the rest of the suite (it runs the real golden scenarios
    # and the real stub-based suite, twice, under coverage) but is still the
    # acceptance bar this tool's own PR is judged on, so it stays unmarked
    # and always-on rather than opted out by default.
    proc = subprocess.run(
        [sys.executable, str(GATE), "--base", "HEAD", "--head", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-6000:]
    assert "PASS" in proc.stdout
