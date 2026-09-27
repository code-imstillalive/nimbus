"""AST size ratchet for the `solver_writer.py` decomposition.

Plan: docs/architecture/tech-debt-plan.md, section 3 "Gates every refactor PR
must pass" -- the "Size" row. Spec: docs/specs/000-golden-master-and-gates.md,
Part C: "AST: fails on a new function over 60 lines or a rise in the count
over 60 (67 at `d5b044b`)."

Measures every MODULE-LEVEL `def`/`async def` (a direct child of the module
body -- not nested in a class or another function) in each `.py` file under a
target directory. A function's size is `end_lineno - lineno + 1`. Both
choices reproduce `tests/analyse_module_dependencies.py`'s own conventions in
this repo (`_module_level_names`'s `tree.body` walk, `_size()`'s line-count
formula) rather than inventing a second one, and the module-level choice is
confirmed correct against real numbers below, not assumed.

## The 67 -> 61, and why this tool's default target is not "the whole package"

Reproduced exactly against the tech-debt plan's own table (measured at
`d5b044b`, this repo's real commit): `custom_components/nimbus_load/
solver_writer.py` alone had 67 module-level functions over 60 lines,
together 12,638 lines -- both numbers matched the plan's table verbatim when
this tool was pointed at that one file, and that citation is a historical
fact about that commit, kept as-is.

nimbus issue #1301 (Phase 2, spec 001, #1347) is the first phase to actually
move code out of the file that this ratchet's own module docstring already
anticipated: six of solver_writer.py's own over-60-line functions
(`resolve_effective_capacity_kwh`, `ha_post_state`, `fetch_p2p_fixed_export_
kw`, `fetch_entity_history_range`, `fetch_entity_attribute_history_range`,
`resample_history_mean`) relocated verbatim to the new `solver_shared.py`,
dropping solver_writer.py's own scoped count to **61**, confirmed by running
this tool against current `main` while making this change (not assumed).
The WHOLE-PACKAGE total is unchanged at **151** -- the six moved within the
same package, so nothing left the count that mode already includes.

`--baseline-count`'s default (61) matches the plan's own solver_writer.py-
scoped number as of this phase, so the natural single-tree invocation for
this gate's actual purpose is:

    python tests/gates/size_ratchet.py custom_components/nimbus_load/solver_writer.py

A bare invocation with no path (whole-package default) will legitimately
report 151 against a baseline of 61 and fail -- that is not this tool being
wrong, it is 61 being the wrong ceiling for that broader scope. Pass
`--baseline-count 151` (or, better, use ratchet mode below, which does not
need either number typed in by hand) if the whole package is really what is
being measured.

## Two modes

**Ratchet mode** (`--base <ref> --head <ref>`, the same shape
`coverage_compare.py` uses): checks out both refs as git worktrees, measures
each, and fails if `head` is worse than `base` in either sense the spec
names -- the over-60 count rises, or an individual function crosses 60 lines
it was not over at `base`. This is self-consistent: it never needs 67 (or
151) re-typed by hand, so it stays correct as the tech-debt plan lowers the
real number over time. This is the mode a refactor PR's CI job should run.

**Single-tree mode** (`--baseline-count N`, default 61, no `--base`/`--head`):
measures the given paths as they sit on disk right now, no git involved, and
fails if the count exceeds N. Useful for a quick local check or for pinning
one specific file's count against a documented number.

## Matching a function across base and head (ratchet mode only)

A pure code-motion phase -- the tech-debt plan's own stated method for most
of it -- moves an already-over-60-line function to a new file without
shrinking it. That must not read as "a new function over 60 lines," which
would make this gate fight the plan it exists to serve. Identity is
`(relative path, name)` first; a name absent from its old file at `head` but
present, at the SAME size, under the same name in some OTHER file at `head`
is treated as MOVED, not new.

This can be fooled by two unrelated functions sharing a name in different
files (7 real cases exist in this package today, e.g. `_schema` repeated
across several `flows/*_subentry.py` files) -- if one shrank below 60 lines
while an unrelated same-named function elsewhere happened to grow past it in
the same PR, this tool could read that as a move. None of the 7 real
duplicate names in this package are anywhere near 60 lines today, so the
practical risk is low; a reviewer should still treat any "moved" verdict
involving a duplicated name as worth a manual look rather than trusting it
blindly.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TARGET = REPO / "custom_components" / "nimbus_load"
DEFAULT_THRESHOLD = 60
DEFAULT_BASELINE_COUNT = (
    61  # solver_writer.py alone as of nimbus #1301/spec 001 -- see module
    # docstring (67 at d5b044b, before six functions moved to
    # solver_shared.py).
)


@dataclass(frozen=True)
class FunctionRecord:
    path: str  # POSIX, relative to the scanned root's parent (stable across worktrees)
    name: str
    lineno: int
    size: int


def _size(node: ast.AST) -> int:
    """`end_lineno - lineno + 1`, `analyse_module_dependencies.py`'s own `_size()`."""
    end = getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)
    return end - getattr(node, "lineno", 0) + 1


def _py_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return [
        p
        for p in sorted(target.rglob("*.py"))
        if "__pycache__" not in p.parts and "research" not in p.parts
    ]


def measure(target: Path, *, relative_to: Path | None = None) -> list[FunctionRecord]:
    """Every module-level function in `target` (a file or a directory).

    `relative_to` fixes the base a file's recorded path is relative to (the
    scanned root's parent by default), so the same function checked out into
    two different worktree directories still gets the same `path` string and
    can be matched across them.
    """
    relative_to = relative_to or target.parent
    records: list[FunctionRecord] = []
    for path in _py_files(target):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            print(f"!! could not parse {path}: {exc}", file=sys.stderr)
            continue
        rel = path.resolve().relative_to(relative_to.resolve()).as_posix()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                records.append(FunctionRecord(rel, node.name, node.lineno, _size(node)))
    return records


def over(records: list[FunctionRecord], threshold: int) -> list[FunctionRecord]:
    return [r for r in records if r.size > threshold]


# ---------------------------------------------------------------------------
# Ratchet mode: compare two measured trees.
# ---------------------------------------------------------------------------


@dataclass
class Regression:
    kind: str  # "new_over_threshold" | "count_rise"
    detail: str


def compare(
    base: list[FunctionRecord], head: list[FunctionRecord], threshold: int
) -> list[Regression]:
    base_over = over(base, threshold)
    head_over = over(head, threshold)

    base_by_key = {(r.path, r.name): r for r in base}
    base_by_name: dict[str, list[FunctionRecord]] = {}
    for r in base:
        base_by_name.setdefault(r.name, []).append(r)

    # A base function "used up" by a move match, so it cannot also explain a
    # second head function of the same name (see module docstring's caveat).
    consumed: set[tuple[str, str]] = set()

    regressions: list[Regression] = []
    for r in head_over:
        prior = base_by_key.get((r.path, r.name))
        if prior is not None:
            if prior.size <= threshold:
                regressions.append(
                    Regression(
                        "new_over_threshold",
                        f"{r.path}:{r.name} grew from {prior.size} to {r.size} lines",
                    )
                )
            continue

        # Not at the same (path, name) at base. Best-effort: the same name,
        # unchanged size, somewhere else at base -> a move, not new.
        candidates = [
            c
            for c in base_by_name.get(r.name, [])
            if c.size == r.size and (c.path, c.name) not in consumed
        ]
        if candidates:
            consumed.add((candidates[0].path, candidates[0].name))
            continue

        regressions.append(
            Regression(
                "new_over_threshold",
                f"{r.path}:{r.name} ({r.size} lines) is newly over {threshold}, "
                f"with no matching function of that name and size at base",
            )
        )

    if len(head_over) > len(base_over):
        regressions.append(
            Regression(
                "count_rise",
                f"functions over {threshold} lines rose from {len(base_over)} to "
                f"{len(head_over)}",
            )
        )

    return regressions


# ---------------------------------------------------------------------------
# git worktree plumbing -- same pattern as coverage_compare.py, deliberately
# duplicated rather than shared: the two tools ship as independent PRs from
# the same base, and a shared helper module would couple them at merge time
# for a ~15-line function.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_paths(paths: list[str]) -> list[Path]:
    """Plain `Path(p)` -- relative to the current working directory, the
    same convention `analyse_module_dependencies.py --module` uses."""
    return [Path(p) for p in paths]


def run_ratchet(
    base_ref: str, head_ref: str, rel_paths: list[str], threshold: int
) -> int:
    with tempfile.TemporaryDirectory(prefix="size-ratchet-") as tmp:
        tmp_path = Path(tmp)
        base_dir = add_worktree(base_ref, tmp_path, "base")
        head_dir = add_worktree(head_ref, tmp_path, "head")
        try:
            base_records: list[FunctionRecord] = []
            head_records: list[FunctionRecord] = []
            for rel in rel_paths:
                base_records += measure(base_dir / rel, relative_to=base_dir)
                head_records += measure(head_dir / rel, relative_to=head_dir)

            regressions = compare(base_records, head_records, threshold)
            base_n = len(over(base_records, threshold))
            head_n = len(over(head_records, threshold))
            print(f"base ({base_ref}):  {base_n} function(s) over {threshold} lines")
            print(f"head ({head_ref}):  {head_n} function(s) over {threshold} lines")
            if not regressions:
                print("PASS: no new function over the limit, no rise in the count.")
                return 0
            print(f"\nFAIL: {len(regressions)} regression(s):")
            for reg in regressions:
                print(f"  [{reg.kind}] {reg.detail}")
            return 1
        finally:
            remove_worktree(base_dir)
            remove_worktree(head_dir)


def run_single_tree(paths: list[Path], threshold: int, baseline_count: int) -> int:
    records: list[FunctionRecord] = []
    for p in paths:
        records += measure(p)
    over_records = over(records, threshold)
    print(
        f"{len(over_records)} function(s) over {threshold} lines (baseline {baseline_count}):"
    )
    for r in sorted(over_records, key=lambda r: (-r.size, r.path, r.name)):
        print(f"  {r.size:5d}  {r.path}:{r.name}")
    if len(over_records) > baseline_count:
        print(
            f"\nFAIL: {len(over_records)} > baseline {baseline_count}. "
            "See this module's own docstring if the target was the whole "
            "package rather than solver_writer.py -- 61 is not that scope's "
            "own baseline."
        )
        return 1
    print(f"\nPASS: {len(over_records)} <= baseline {baseline_count}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        help="files/directories to scan (default: the whole custom_components/nimbus_load package)",
    )
    parser.add_argument("--base", help="git ref to compare from (enables ratchet mode)")
    parser.add_argument("--head", help="git ref to compare to (enables ratchet mode)")
    parser.add_argument(
        "--baseline-count",
        type=int,
        default=DEFAULT_BASELINE_COUNT,
        help=f"single-tree mode only: max allowed count (default {DEFAULT_BASELINE_COUNT}, "
        "solver_writer.py's own count at d5b044b -- see module docstring if scanning "
        "the whole package instead)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"line-count limit (default {DEFAULT_THRESHOLD}, the tech-debt plan's own "
        "figure; override only to test this tool itself)",
    )
    args = parser.parse_args(argv)

    if bool(args.base) != bool(args.head):
        parser.error("--base and --head must be given together")

    if args.base and args.head:
        rel_paths = args.paths or ["custom_components/nimbus_load"]
        return run_ratchet(args.base, args.head, rel_paths, args.threshold)

    targets = _resolve_paths(args.paths) if args.paths else [DEFAULT_TARGET]
    for t in targets:
        if not t.exists():
            print(f"no such path: {t}", file=sys.stderr)
            return 2
    return run_single_tree(targets, args.threshold, args.baseline_count)


if __name__ == "__main__":
    raise SystemExit(main())
