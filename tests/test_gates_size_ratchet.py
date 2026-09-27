"""Proves `tests/gates/size_ratchet.py` on a deliberately bad synthetic case,
per docs/specs/000-golden-master-and-gates.md Part C and the tech-debt plan's
"Part C tools, one PR each, each proven on a deliberately bad commit."

Synthetic fixtures only for the bad/good pairs (no `custom_components/
nimbus_load` fixture, per the tech-debt plan's own instruction that a gate's
proof should be fast and independent of the real codebase). One real check
at the end confirms the tool does not false-positive against the actual
repo, and documents the real numbers it reports today.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent / "gates" / "size_ratchet.py"
REPO = Path(__file__).resolve().parents[1]


def _fn(name: str, body_lines: int) -> str:
    """A syntactically valid module-level function with exactly `body_lines`
    lines in its body, so the whole `def` block is `body_lines + 1` lines
    (the `def` line itself)."""
    lines = [f"def {name}():"]
    lines += [f"    x = {i}  # line {i}" for i in range(body_lines)]
    lines.append("    return x")
    return "\n".join(lines) + "\n"


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Single-tree mode: synthetic bad / good.
# ---------------------------------------------------------------------------


def test_single_tree_mode_fails_on_a_70_line_function(tmp_path: Path) -> None:
    bad = tmp_path / "bad_module.py"
    # `_fn` emits `def` + body_lines + `return` = body_lines + 2 total lines.
    bad.write_text(_fn("does_too_much", 68))  # 70 lines total

    proc = _run(["--baseline-count", "0", str(bad)])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "does_too_much" in proc.stdout
    assert "70" in proc.stdout
    assert "FAIL" in proc.stdout


def test_single_tree_mode_passes_on_a_50_line_function(tmp_path: Path) -> None:
    good = tmp_path / "good_module.py"
    good.write_text(_fn("does_one_thing", 48))  # 50 lines total, under 60

    proc = _run(["--baseline-count", "0", str(good)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout


def test_single_tree_mode_respects_a_nonzero_baseline(tmp_path: Path) -> None:
    """One over-limit function against baseline 1 passes; against baseline 0
    it fails -- the count comparison, not just "any violation", is exercised."""
    mod = tmp_path / "one_big.py"
    mod.write_text(_fn("big", 69))

    assert _run(["--baseline-count", "1", str(mod)]).returncode == 0
    assert _run(["--baseline-count", "0", str(mod)]).returncode == 1


# ---------------------------------------------------------------------------
# Ratchet mode: a synthetic two-commit git repo, base vs a deliberately bad
# head, and base vs a legitimately refactored (moved, not grown) head.
# ---------------------------------------------------------------------------


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
def toy_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway git repo the gate's `--base`/`--head` worktrees are cut
    from. `size_ratchet.REPO` is patched to point at it so the gate's own
    `git worktree add` runs against this toy history, never the real nimbus
    checkout."""
    root = tmp_path / "toy"
    root.mkdir()
    _init_repo(root)
    return root


def _run_ratchet(
    root: Path, base: str, head: str, *paths: str
) -> subprocess.CompletedProcess:
    env_args = ["--base", base, "--head", head, *paths]
    # size_ratchet.py resolves worktrees against its own REPO constant
    # (parents[2] of the real gates/size_ratchet.py), which is this real
    # checkout, not the toy repo -- so it is pointed at the toy repo the
    # same way a real invocation points anywhere: `git worktree add` runs
    # with a `cwd` of whatever repo the ref lives in. Patching in-process
    # is not available across a subprocess boundary, so this drives the
    # module directly instead of via `python size_ratchet.py`.
    return subprocess.run(
        [sys.executable, "-c", _RATCHET_RUNNER, str(root), *env_args],
        capture_output=True,
        text=True,
        check=False,
    )


_RATCHET_RUNNER = f"""
import sys
sys.path.insert(0, {str(GATE.parent)!r})
import size_ratchet

repo, args = sys.argv[1], sys.argv[2:]
size_ratchet.REPO = __import__("pathlib").Path(repo)
sys.exit(size_ratchet.main(args))
"""


def test_ratchet_mode_fails_when_a_function_grows_past_the_limit(
    toy_repo: Path,
) -> None:
    mod = toy_repo / "pkg" / "mod.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(_fn("worker", 48))  # 50 lines, under 60
    _commit(toy_repo, "base: worker under the limit")

    mod.write_text(_fn("worker", 69))  # grown to 71 lines, over the limit
    _commit(toy_repo, "head: worker grown past the limit")

    proc = _run_ratchet(toy_repo, "HEAD~1", "HEAD", "pkg")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "new_over_threshold" in proc.stdout
    assert "worker" in proc.stdout


def test_ratchet_mode_fails_when_the_over_limit_count_rises(toy_repo: Path) -> None:
    mod = toy_repo / "pkg" / "mod.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(_fn("first_big", 69))
    _commit(toy_repo, "base: one over-limit function")

    mod.write_text(_fn("first_big", 69) + "\n" + _fn("second_big", 69))
    _commit(toy_repo, "head: a second over-limit function added")

    proc = _run_ratchet(toy_repo, "HEAD~1", "HEAD", "pkg")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "count_rise" in proc.stdout


def test_ratchet_mode_passes_when_an_over_limit_function_only_moves(
    toy_repo: Path,
) -> None:
    """The tech-debt plan's own method: an already-over-60-line function
    relocates to a new file, unchanged in size. This must not read as
    "a new function over 60 lines" -- that would make the gate fight the
    refactor it exists to permit."""
    old = toy_repo / "pkg" / "old_home.py"
    old.parent.mkdir(parents=True)
    old.write_text(_fn("relocated", 69))
    _commit(toy_repo, "base: relocated lives in old_home.py")

    old.write_text("")  # moved out
    new = toy_repo / "pkg" / "new_home.py"
    new.write_text(_fn("relocated", 69))  # identical size, new file
    _commit(toy_repo, "head: relocated moved to new_home.py, unchanged")

    proc = _run_ratchet(toy_repo, "HEAD~1", "HEAD", "pkg")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout


def test_ratchet_mode_passes_comparing_a_ref_to_itself(toy_repo: Path) -> None:
    mod = toy_repo / "pkg" / "mod.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(_fn("worker", 69))
    _commit(toy_repo, "one commit")

    proc = _run_ratchet(toy_repo, "HEAD", "HEAD", "pkg")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout


# ---------------------------------------------------------------------------
# One real check against the actual repo (not synthetic), per the tech-debt
# plan's acceptance bar: prove the tool does not false-positive on the real
# codebase, and document the real number.
# ---------------------------------------------------------------------------


def test_real_solver_writer_matches_the_tech_debt_plans_own_count() -> None:
    """docs/architecture/tech-debt-plan.md's own table, measured at `d5b044b`:
    "Functions over 60 lines | 67, together 12,638 lines" for
    `solver_writer.py` alone. That number changed for the first time under
    nimbus issue #1301: spec 001 (#1347) first moved six over-60-line
    functions (`resolve_effective_capacity_kwh`, `ha_post_state`,
    `fetch_p2p_fixed_export_kw`, `fetch_entity_history_range`,
    `fetch_entity_attribute_history_range`, `resample_history_mean`)
    verbatim to `solver_shared.py`, dropping the count 67 -> 61. Phase
    2b/2c then moved eight reporting functions to `solver_reports/`, five
    of which were themselves over the limit
    (`_compute_report_for_window`, `publish_daily_quality_report`,
    `_soc_discrepancy_stats`, `rescore_quality_history`,
    `_carry_forward_quality_history`), dropping it 61 -> **53**.

    Reproduced here against this checkout so a future session can see at a
    glance whether the number is still accurate, per the tool's own
    docstring caveat that it changes as later phases move more code out.
    If this fails, the plan's own table and the tool's
    `DEFAULT_BASELINE_COUNT` need updating, not this test -- and note the
    ratchet only ever tightens, so a count BELOW the baseline is progress
    that should be banked here, not a failure to work around."""
    target = REPO / "custom_components" / "nimbus_load" / "solver_writer.py"
    proc = _run(["--baseline-count", "53", str(target)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "53 function(s) over 60 lines" in proc.stdout, proc.stdout


def test_real_whole_package_is_a_materially_different_larger_number() -> None:
    """Documents the discrepancy this tool's own docstring calls out: the
    whole-package default scope is not 61. Asserts only that it is larger
    (not an exact count, which 40 unrelated files make too brittle to pin
    here) so this test does not need updating every time an unrelated file
    in the package changes shape."""
    proc = _run(
        ["--baseline-count", "53", str(REPO / "custom_components" / "nimbus_load")]
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "> baseline 53" in proc.stdout, proc.stdout
