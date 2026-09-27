"""Coverage-regression gate for the `solver_writer.py` decomposition.

Plan: docs/architecture/tech-debt-plan.md, section 3 "Gates every refactor PR
must pass" -- the "Coverage" row. Spec: docs/specs/000-golden-master-and-
gates.md, Part C: "runs the golden scenarios and the suite under coverage on
the base and head commits; fails if any line executed on base is unexecuted
on head, mapped through the spec's moved-code list."

    python tests/gates/coverage_compare.py --base <ref> --head <ref> \\
        [--moved-code path/to/mapping.json]

## What it runs, and why in two separate steps

Every golden scenario (`tests/golden/scenarios.py`) drives `main()` in its
own subprocess (`golden.harness.run_isolated`'s own reasoning: module state,
a known test-order dependence, floating-point pins that must predate numpy's
import -- see that module's docstring). This tool reproduces that same
subprocess shape, wrapped in `coverage run --parallel-mode`, using the ref's
OWN copy of `golden.harness.child_env()` for the environment rather than
duplicating it, so a future change to the harness's env plumbing is picked
up here for free instead of silently drifting out of sync. Then the full
stub-based suite runs the same way, one more `coverage run --parallel-mode`
invocation. All of it lands in one combined `coverage combine` per side.

## What "coverage on `solver_writer.py`" means across a refactor

A refactor moves code OUT of `solver_writer.py`; comparing that file's line
NUMBERS before and after is meaningless the moment a single line is inserted
or removed above the code that matters. This tool matches by two rules, in
order, for every line covered at `base`:

1. **Same position, same text.** The line number is unchanged, and the
   source text at that position is unchanged. The common case for any file
   the phase in question is not touching.
2. **Moved: same text, covered somewhere in the target file or one of
   `--moved-code`'s destination files.** Matched against a pool of covered
   lines' stripped source text, each occurrence consumed once it is used --
   one real line in the new location cannot silently excuse several
   different lines that actually went missing. Lines already claimed by
   rule 1 are removed from the pool first, so a repositioned line cannot
   double-count as evidence for an unrelated line that merely has the same
   text (e.g. a bare `return None`).

This is exactly the "best-effort match, since line numbers shift" the spec
asks for, not exact line-to-line accounting -- and it directly follows from
the tech-debt plan's own constraint on what an extraction is allowed to be
(step 3: "No test assertion changes... behavior-preserving"): a compliant
move relocates a line's literal source text unchanged, which is precisely
what rule 2 can see. A genuine rewrite of the moved code is exactly the case
this cannot vouch for, which is the right failure direction for a gate.

**Known limitation, worth a reviewer's attention**: two unrelated, already-
covered lines that happen to share identical stripped source text (a common
short call or return statement, repeated verbatim elsewhere in the same
file) can match each other instead of the line that actually moved. The
per-occurrence consumption above bounds the damage -- it cannot manufacture
matches that were never covered at head at all -- but it cannot always
attribute a match to the RIGHT line. Treat any regression this tool
reports as authoritative (something covered at base is not covered
anywhere plausible at head); treat a clean run on a large or heavily
edited diff with a little more scrutiny than a small one.

## `--moved-code`

Optional. A JSON file: `{"moved_to": ["custom_components/nimbus_load/
solver_inputs/whatever.py", ...]}` -- the destination files a phase's own
spec says code moved to. Without it, this tool compares `solver_writer.py`'s
own coverage directly (rule 1 only reaches lines that did not move; rule 2's
pool is then just `solver_writer.py` against itself, which still catches
"used to run, no longer runs, and did not move anywhere else in this same
file" -- useful on its own, per the spec).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_FILE = "custom_components/nimbus_load/solver_writer.py"
DEFAULT_SUITE_ARGS = [
    "tests/",
    "--ignore=tests/hass_integration/",
    "-p",
    "no:homeassistant",
    "-q",
]

# Run in a fresh child, wrapped in `coverage run --parallel-mode`, for every
# golden scenario a ref's OWN `tests/golden` defines. Deliberately reads
# `golden.harness.child_env()` from THAT ref rather than duplicating its
# environment here, so this tool cannot silently drift from what the harness
# actually needs (state file paths, floating-point pins, HA_BASE/HA_TOKEN).
_GOLDEN_DRIVER = r"""
import subprocess, sys, tempfile
from pathlib import Path

tests_dir, data_file, source_arg = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, tests_dir)
try:
    from golden import harness, scenarios
except ImportError as exc:
    sys.stderr.write(
        f"tests/golden not importable from {tests_dir!r} ({exc}); "
        "coverage_compare.py requires both --base and --head to be at or "
        "after spec 000 (docs/specs/000-golden-master-and-gates.md).\n"
    )
    sys.exit(1)

repo = Path(tests_dir).parent
ran = []
for name in scenarios.names():
    cycles = scenarios.get(name).cycles
    with tempfile.TemporaryDirectory(prefix=f"gate-golden-{name}-") as tmp:
        workdir = Path(tmp)
        env = harness.child_env(workdir)
        out = workdir / "record.json"
        cmd = [
            sys.executable, "-m", "coverage", "run", "--parallel-mode",
            f"--data-file={data_file}", f"--source={source_arg}",
            "-m", "golden.child", name, str(out), str(cycles),
        ]
        proc = subprocess.run(
            cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=600
        )
        if proc.returncode != 0:
            sys.stderr.write(f"golden scenario {name!r} failed under coverage:\n")
            sys.stderr.write(proc.stdout[-4000:] + "\n" + proc.stderr[-8000:] + "\n")
            sys.exit(1)
        ran.append(name)
print(f"  golden: ran {len(ran)} scenario(s) under coverage: {', '.join(ran)}")
"""


def add_worktree(ref: str, into: Path, label: str) -> Path:
    """`label` (not the ref) names the directory: `--base X --head X` is a
    legitimate sanity check (see this tool's own tests), and two worktrees
    for the same ref must not collide on the same destination path."""
    dest = into / f"wt-{label}"
    proc = subprocess.run(
        ["git", "worktree", "add", "--detach", str(dest), ref],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git worktree add --detach {dest} {ref} failed:\n{proc.stderr}"
        )
    return dest


def remove_worktree(path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )


def run_golden_scenarios_under_coverage(
    worktree: Path, data_file: Path, source_arg: str
) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _GOLDEN_DRIVER,
            str(worktree / "tests"),
            str(data_file),
            source_arg,
        ],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        raise RuntimeError(
            f"golden-scenario coverage run failed in {worktree}:\n{proc.stdout}\n{proc.stderr}"
        )


def run_pytest_suite_under_coverage(
    worktree: Path, data_file: Path, source_arg: str
) -> None:
    cmd = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        "--parallel-mode",
        f"--data-file={data_file}",
        f"--source={source_arg}",
        "-m",
        "pytest",
        *DEFAULT_SUITE_ARGS,
    ]
    proc = subprocess.run(
        cmd, cwd=worktree, capture_output=True, text=True, timeout=3600, check=False
    )
    tail = "\n".join(proc.stdout.strip().splitlines()[-3:])
    print(f"  suite:  {tail}")
    # 0 = every test passed, 1 = some failed -- either way the run completed
    # and produced real coverage data; a failing test is the "Tests" gate's
    # job to catch, not this one's. Anything else (2/3/4/5: usage error,
    # interrupted, internal error, no tests collected) means no trustworthy
    # coverage data came out of this at all.
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"pytest suite errored (exit {proc.returncode}) in {worktree}:\n"
            f"{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"
        )


def run_custom_commands(
    worktree: Path, data_file: Path, source_arg: str, commands: list[str]
) -> None:
    """Used only by this tool's own tests, to exercise the worktree/coverage/
    combine/compare machinery against a synthetic fixture instead of the
    real nimbus golden harness and suite (which the tech-debt plan's own
    acceptance bar asks NOT to depend on for a gate tool's proof)."""
    for command in commands:
        cmd = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            f"--data-file={data_file}",
            f"--source={source_arg}",
            *shlex.split(command),
        ]
        proc = subprocess.run(
            cmd, cwd=worktree, capture_output=True, text=True, timeout=600, check=False
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"--exercise-command {command!r} failed (exit {proc.returncode}) in "
                f"{worktree}:\n{proc.stdout}\n{proc.stderr}"
            )


def combine_and_extract(
    worktree: Path, data_file: Path, rcfile: Path, target_files: list[str]
) -> dict[str, set[int]]:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "combine",
            f"--rcfile={rcfile}",
            f"--data-file={data_file}",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    out_json = data_file.parent / (data_file.name + ".json")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            f"--rcfile={rcfile}",
            f"--data-file={data_file}",
            "-o",
            str(out_json),
            "-i",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    files = payload.get("files", {})
    return {
        rel: set(files[rel]["executed_lines"]) for rel in target_files if rel in files
    }


def _source_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def find_regressions(
    base_dir: Path,
    head_dir: Path,
    source_file: str,
    moved_to: list[str],
    base_cov: dict[str, set[int]],
    head_cov: dict[str, set[int]],
) -> list[str]:
    base_lines = _source_lines(base_dir / source_file)
    head_lines = _source_lines(head_dir / source_file)
    base_covered = sorted(base_cov.get(source_file, set()))
    head_covered_source = head_cov.get(source_file, set())

    def text_at(lines: list[str], lineno: int) -> str | None:
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1].strip()
        return None

    # Rule 1 first, so an exact-position match's own head line is removed
    # from the pool before rule 2 runs -- otherwise it could double as
    # "evidence" for a second, unrelated base line with identical text.
    exact_matched: set[int] = set()
    unresolved: list[int] = []
    for ln in base_covered:
        base_text = text_at(base_lines, ln)
        if ln in head_covered_source and text_at(head_lines, ln) == base_text:
            exact_matched.add(ln)
        else:
            unresolved.append(ln)

    pool: Counter[str] = Counter()
    for fname in {source_file, *moved_to}:
        lines = _source_lines(head_dir / fname)
        for lineno in head_cov.get(fname, set()):
            if fname == source_file and lineno in exact_matched:
                continue
            text = text_at(lines, lineno)
            if text:
                pool[text] += 1

    regressions: list[str] = []
    for ln in unresolved:
        text = text_at(base_lines, ln) or ""
        if text and pool.get(text, 0) > 0:
            pool[text] -= 1
            continue
        regressions.append(
            f"{source_file}:{ln}: {text!r} covered at base, not covered anywhere "
            "plausible at head"
        )
    return regressions


def run(
    base_ref: str,
    head_ref: str,
    source_file: str,
    moved_to: list[str],
    exercise_commands: list[str] | None,
) -> int:
    with tempfile.TemporaryDirectory(prefix="coverage-compare-") as tmp:
        tmp_path = Path(tmp)
        rcfile = tmp_path / "gate.coveragerc"
        rcfile.write_text("[run]\nrelative_files = True\n", encoding="utf-8")

        base_dir = add_worktree(base_ref, tmp_path, "base")
        head_dir = add_worktree(head_ref, tmp_path, "head")
        try:
            source_dirs = sorted(
                {str(Path(f).parent) for f in [source_file, *moved_to]}
            )
            source_arg = ",".join(source_dirs)

            base_data = tmp_path / "cov-base" / "data"
            head_data = tmp_path / "cov-head" / "data"
            base_data.parent.mkdir()
            head_data.parent.mkdir()

            for label, worktree, data_file in (
                ("base", base_dir, base_data),
                ("head", head_dir, head_data),
            ):
                print(f"[{label}] {worktree}")
                if exercise_commands:
                    run_custom_commands(
                        worktree, data_file, source_arg, exercise_commands
                    )
                else:
                    run_golden_scenarios_under_coverage(worktree, data_file, source_arg)
                    run_pytest_suite_under_coverage(worktree, data_file, source_arg)

            target_files = [source_file, *moved_to]
            base_cov = combine_and_extract(base_dir, base_data, rcfile, target_files)
            head_cov = combine_and_extract(head_dir, head_data, rcfile, target_files)

            base_n = len(base_cov.get(source_file, set()))
            head_n = len(head_cov.get(source_file, set()))
            print(f"\n{source_file}: {base_n} lines covered at base, {head_n} at head")
            if moved_to:
                print(f"moved-code destinations: {', '.join(moved_to)}")

            regressions = find_regressions(
                base_dir, head_dir, source_file, moved_to, base_cov, head_cov
            )
            if not regressions:
                print(
                    "PASS: every line covered at base is covered somewhere plausible at head."
                )
                return 0
            print(f"\nFAIL: {len(regressions)} line(s) regressed:")
            for reg in regressions:
                print(f"  {reg}")
            return 1
        finally:
            remove_worktree(base_dir)
            remove_worktree(head_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument("--base", required=True, help="git ref to compare from")
    parser.add_argument("--head", required=True, help="git ref to compare to")
    parser.add_argument(
        "--moved-code",
        type=Path,
        default=None,
        help='JSON file: {"moved_to": ["custom_components/nimbus_load/..."]}',
    )
    parser.add_argument(
        "--source-file",
        default=DEFAULT_SOURCE_FILE,
        help=f"file whose coverage is compared (default {DEFAULT_SOURCE_FILE})",
    )
    parser.add_argument(
        "--exercise-command",
        action="append",
        default=None,
        help="override the default golden-scenarios+suite run with an explicit command "
        "(may repeat); for this tool's own tests against a synthetic fixture, not for "
        "real use against nimbus",
    )
    args = parser.parse_args(argv)

    moved_to: list[str] = []
    if args.moved_code:
        payload = json.loads(args.moved_code.read_text(encoding="utf-8"))
        moved_to = list(payload.get("moved_to", []))

    return run(args.base, args.head, args.source_file, moved_to, args.exercise_command)


if __name__ == "__main__":
    raise SystemExit(main())
