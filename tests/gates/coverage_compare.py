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
import os
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_FILE = "custom_components/nimbus_load/solver_writer.py"

# nimbus #1354, finding 1: "every line covered at base is covered somewhere
# plausible at head" is vacuously true when the base side measured ZERO
# covered lines -- so a run that measured nothing was, before this, printing
# the same PASS and returning the same 0 as a run that measured everything
# and found no regression. Distinct from both PASS (0) and a real regression
# (1): a base side with no measured coverage is refused outright, loudly,
# rather than silently blessed.
NO_BASE_COVERAGE_EXIT_CODE = 2

# A REAL incident, found live while opening this PR: this tool's own
# `--base HEAD --head HEAD` default pipeline runs `pytest tests/` inside a
# worktree, which collects tests/test_gates_coverage_compare.py -- including
# `test_real_main_compared_to_itself_has_zero_regressions`, which invokes
# THIS SAME SCRIPT again. Each level checks out its own pair of worktrees and
# runs the full suite again, recursing without a base case: caught only
# because it visibly spawned dozens of nested `git worktree`s and coverage-
# instrumented pytest runs on a live host (including a second, independent
# session hitting the same thing while reviewing this very PR) before this
# fix landed. Two independent guards, deliberately not just one:
_RECURSION_GUARD_ENV = "NIMBUS_COVERAGE_COMPARE_ACTIVE"
DEFAULT_SUITE_ARGS = [
    "tests/",
    "--ignore=tests/hass_integration/",
    # Guard 1: this tool's own meta-tests are about the tool, not about
    # solver_writer.py, and running them as part of "the suite" is exactly
    # what the incident above was. Excluded from the run THIS tool drives,
    # not from the suite in general -- a normal top-level `pytest tests/`
    # still collects and runs them.
    "--ignore=tests/test_gates_coverage_compare.py",
    "--ignore=tests/test_gates_size_ratchet.py",
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


def canonical_worktree_root(raw: str | Path) -> Path:
    """Resolve `raw` to the same form coverage.py uses when it canonicalises
    the files it measures.

    nimbus #1354, finding 2: `tempfile.gettempdir()` (what a fresh
    `TemporaryDirectory()`'s name is built from) can return an 8.3 short
    path on Windows (`C:\\Users\\RAF_LO~1\\...`), while coverage.py
    canonicalises the paths it records via `os.path.realpath`, which
    resolves that same directory to its long form
    (`C:\\Users\\Raf_local\\...`). If the worktree root handed to `coverage
    run --source=...` and the root this tool later looks measured lines up
    under are two different spellings of the same directory, every lookup
    misses -- every file reads 0 lines covered, which finding 1 elsewhere
    in this module then let through as a silent PASS. Routed through one
    function so it can be unit-tested directly (see this module's own
    tests) without needing an actual short-path-aliased filesystem to
    reproduce against.
    """
    return Path(os.path.realpath(raw))


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
    # Guard 2 (belt-and-suspenders alongside the --ignore flags above): mark
    # every process this suite run spawns as "inside a coverage_compare run"
    # so that if anything in it -- this file's own meta-tests, a future test
    # file nobody thought to add to the ignore list, anything -- tries to
    # invoke this script again, `main()`'s own check below refuses instead of
    # recursing.
    env = {**os.environ, _RECURSION_GUARD_ENV: "1"}
    proc = subprocess.run(
        cmd,
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
        env=env,
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
    # nimbus #1354: coverage.py's own JSON report keys each file by the
    # HOST OS's native separator (backslash on Windows) even under
    # `relative_files = True`, while `target_files` (--source-file /
    # --moved-code, and DEFAULT_SOURCE_FILE) always use forward slashes --
    # this project's own git-path convention. On Windows, a raw string
    # lookup of a forward-slash key into these backslash-keyed results
    # never matches, so every file reads 0 covered lines regardless of
    # whether the worktree root itself is canonicalised (see
    # canonical_worktree_root) -- confirmed directly: this still measured
    # 0 lines against a worktree root that had already been resolved to
    # its long form. Normalize both sides to forward slashes once, here,
    # so the lookup is OS-independent.
    normalized_files = {Path(rel).as_posix(): info for rel, info in files.items()}
    return {
        rel: set(normalized_files[rel]["executed_lines"])
        for rel in target_files
        if rel in normalized_files
    }


def _source_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


# nimbus issue #1411: the matching key below is the line's TEXT, and every
# extraction phase from 2a onward REWRITES the lines it moves -- a moved body
# reaches back into `solver_writer` as `sw.<name>` and reaches the shared logger
# as `solver_shared._LOGGER`, because that deferred seam is how a lower-layer
# module reads those without an upward import. Raw text therefore stops matching
# and the line reads as uncovered, which made this gate unable to verify the one
# operation it was built for.
#
# Measured on Phase 3 (#1302): 33 lines reported as regressed, all 33 artifacts.
# `if _NATIVE_HASS is None:` occurred **0** times in the head tree while
# `if sw._NATIVE_HASS is None:` occurred **4** -- the code ran, the gate was
# looking for a string that no longer existed.
#
# The normalisation is deliberately NARROW: exactly the two prefixes the
# extraction introduces, elided at an identifier boundary. A general "strip any
# `<name>.`" rule would also flatten ordinary attribute access (`plan.status` ->
# `status`), inventing matches between unrelated lines and weakening the key
# everywhere to fix it in two places.
#
# If a future phase introduces a third seam alias, add it here -- and note that
# this list failing to keep up degrades toward FALSE POSITIVES (a real line
# reported as regressed), never toward a missed regression, which is the right
# direction for a gate to fail in.
_QUALIFIER_PREFIXES = ("sw.", "solver_shared.")


def _match_key(line: str) -> str:
    """The text of `line`, with extraction-introduced qualifiers elided.

    Used on BOTH sides of the comparison, so a base line and the moved,
    requalified head line it became produce the same key.
    """
    text = line.strip()
    for prefix in _QUALIFIER_PREFIXES:
        if prefix not in text:
            continue
        out = []
        i = 0
        while i < len(text):
            if text.startswith(prefix, i):
                before = text[i - 1] if i else ""
                # only at an identifier boundary -- never inside a longer name
                if not (before.isalnum() or before == "_" or before == "."):
                    i += len(prefix)
                    continue
            out.append(text[i])
            i += 1
        text = "".join(out)
    return text


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
            return _match_key(lines[lineno - 1])
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
        # nimbus #1354, finding 2 -- see canonical_worktree_root's own
        # docstring. Canonicalise once, here, before this root is handed to
        # coverage or compared against anything, so both sides always agree.
        tmp_path = canonical_worktree_root(tmp)
        rcfile = tmp_path / "gate.coveragerc"
        rcfile.write_text("[run]\nrelative_files = True\n", encoding="utf-8")

        # Both worktrees are created and removed inside the SAME try/finally
        # -- previously `add_worktree` ran before the `try`, so a failure
        # creating `head_dir` (the second call) left `base_dir`'s worktree
        # registered forever: the temp directory backing it still gets
        # deleted when this `with` block exits, but nothing ever ran
        # `git worktree remove` for it, leaving a stale, unprunable
        # registration behind. nimbus #1354 found 7 of these accumulated
        # from one afternoon's runs.
        base_dir: Path | None = None
        head_dir: Path | None = None
        try:
            base_dir = add_worktree(base_ref, tmp_path, "base")
            head_dir = add_worktree(head_ref, tmp_path, "head")
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

            if base_n == 0:
                # nimbus #1354, finding 1. "Every line covered at base is
                # covered at head" holds vacuously over an empty set, so
                # without this check a base side that measured NOTHING
                # (wrong --source-file, a --source typo, an import that
                # never happened, or -- on Windows -- finding 2's own 8.3
                # short-path aliasing between the worktree root and what
                # coverage.py canonicalises) is silently indistinguishable
                # from a base side that measured everything and found no
                # regression. Refuse to report PASS either way: a distinct
                # exit code, never 0 (pass) or 1 (a real regression), so a
                # CI caller can tell "nothing was measured" apart from both.
                print(
                    f"FAIL: measured no coverage at base for {source_file} -- "
                    "refusing to report PASS. See nimbus #1354: this cannot "
                    "be told apart from a genuine zero-regression run without "
                    "this check. Check --source-file/--source, and whether "
                    "the golden scenarios / suite actually imported this "
                    "file at base."
                )
                return NO_BASE_COVERAGE_EXIT_CODE

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
            # Either or both may still be None if add_worktree itself
            # raised -- only remove what was actually created.
            if base_dir is not None:
                remove_worktree(base_dir)
            if head_dir is not None:
                remove_worktree(head_dir)


def main(argv: list[str] | None = None) -> int:
    if os.environ.get(_RECURSION_GUARD_ENV):
        # See the comment on DEFAULT_SUITE_ARGS: a real recursive-invocation
        # incident, caught live. Refuse loudly rather than recurse.
        print(
            f"refusing to run: {_RECURSION_GUARD_ENV} is set, meaning this process "
            "was spawned by another coverage_compare.py run's own suite step. "
            "Running again here would recurse without a base case.",
            file=sys.stderr,
        )
        return 1

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
