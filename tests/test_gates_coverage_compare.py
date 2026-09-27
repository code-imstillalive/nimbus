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
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent / "gates" / "coverage_compare.py"
REPO = Path(__file__).resolve().parents[1]


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
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, str(root), *args],
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
