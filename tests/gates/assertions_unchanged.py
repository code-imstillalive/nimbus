"""Gate: fails if an assertion in an existing test changed.

Spec: docs/specs/000-golden-master-and-gates.md, Part C --
`tests/gates/assertions_unchanged.py`: "fails if an `assert` in an existing
test changed, except lines the spec lists." Plan:
docs/architecture/tech-debt-plan.md, section 3's gate table.

## What this protects against

The tech-debt plan's method (section 3) is specification-driven extraction:
move code, don't change behaviour, and "No test assertion changes" is one of
its own steps. A refactor PR that quietly loosens or retargets what a test
checks -- widening a tolerance, dropping a branch of an `assertIn`, deleting
an inconvenient `assertRaises` -- can still show a green, unchanged pass
count. This gate reads the tests themselves, at both ends of the PR, and
flags any assertion whose CONTENT differs, whether or not the pass/fail
outcome changed.

## What counts as "an assertion" in this codebase

Measured directly (see the worklog entry for the numbers): this suite is
overwhelmingly `unittest.TestCase`-style (2000+ `self.assert*` calls) with a
real minority of bare `assert` statements (also present, in ~fewer files) and
mock-library call assertions (`.assert_called_once()`, `.assert_not_called()`,
etc.). `pytest.raises(...)` appears too. All four shapes are assertions in the
sense this gate cares about -- each one is a claim the test makes about
observed behaviour -- so all four are collected:

- `ast.Assert` (bare `assert ...`, including `assert x, "message"`)
- any call whose attribute name starts with `assert` (`self.assertEqual(...)`,
  `mock_obj.assert_called_once_with(...)`, `self.assertRaisesRegex(...)`, ...)
- any call whose attribute name is exactly `raises` (`pytest.raises(...)`,
  covering both the context-manager and decorator forms -- the call itself is
  what's compared, not the `with` block's body, which is the code under test
  rather than the assertion)

## How "changed" is decided

The spec's own wording is "compare the assertion's own source text ... not
just line number." Taken literally, byte-for-byte text equality would flag a
plain `ruff format` reflow (a wrapped call reformatted onto one more or fewer
lines) as a changed assertion, which is not the failure mode this gate
exists to catch and would make the gate impossible to keep green through a
routine format pass.

So the actual comparison key is structural: `ast.dump()` of the assertion
node with source-location fields stripped, which is insensitive to
whitespace/line-wrapping but sensitive to every value, operator, and
argument in the call. The exact source text (via `ast.get_source_segment`)
is kept alongside for the diagnostic output a human reads -- so the spec's
"source text" is what gets DISPLAYED, structural equality is what gets
COMPARED. Anyone who disagrees with this tradeoff can require line-identical
text by comparing on `.text` instead of `.key` below; it is one line.

## Matching assertions across a diff

Assertions inside one test function are matched between base and head by
STRUCTURAL EQUALITY, aligned in source order via `difflib.SequenceMatcher`
(the same idea `git diff` itself uses for text). This means:

- an `insert` (a wholly new assertion appears, with nothing removed around
  it) is an ADDITION and is never flagged -- the plan explicitly allows a
  refactor to add tests/assertions, never to edit or remove one.
- a `delete` (an assertion present in base has no structural match in head)
  is a REMOVAL and IS flagged -- a removed assertion is exactly the
  "quietly loosened" case this gate exists to catch.
- a `replace` block (base and head both changed) pairs old/new positions
  within the block index-by-index and flags each pair. If the block's
  lengths differ, the extra tail on whichever side is longer is treated as
  an addition (not flagged) if head is longer, or a removal (flagged) if
  base is longer -- see `_diff_test_assertions()`.

## Scope and known limitations

- Only files that exist under `tests/` at BOTH `--base` and `--head` are
  compared. A wholly new test file is out of scope by design (the spec: "a
  wholly new test file is fine").
- A "test function" is any `def`/`async def` whose name starts with `test`,
  either at module level or as a direct method of a class (one level of
  nesting -- this codebase's own convention, confirmed by grep: every
  `test_*` method sits directly inside a `TestCase` subclass or a bare
  pytest test class, never doubly nested). Matching across base/head is by
  file + qualified name (`ClassName.test_method` or `test_function`); a
  RENAMED test function is invisible to this gate (it looks like one test
  removed and an unrelated one added) -- a determined evasion could exploit
  this, and it is named here rather than silently left as a surprise.
- Assertions are collected from a test function's ENTIRE body, including
  inside any nested closures it defines (e.g. a `side_effect` callback
  the test builds inline) -- an `assert` inside such a closure is still
  something the test relies on.
- This reaches only `tests/`, matching Part C's own scope (`custom_components/`
  is never touched by this plan's tooling).
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Assertion:
    """One assertion found inside one test function."""

    qualname: str
    lineno: int
    end_lineno: int
    text: str
    key: str  # ast.dump() with location fields stripped -- the comparison key


@dataclass(frozen=True)
class ChangedAssertion:
    file: str
    qualname: str
    old: Assertion | None
    new: Assertion | None

    def describe(self) -> str:
        loc_old = f"{self.file}:{self.old.lineno}" if self.old else "<none>"
        loc_new = f"{self.file}:{self.new.lineno}" if self.new else "<none>"
        old_text = self.old.text if self.old else "<removed>"
        new_text = self.new.text if self.new else "<removed>"
        return (
            f"{self.file} :: {self.qualname}\n"
            f"  base ({loc_old}): {old_text}\n"
            f"  head ({loc_new}): {new_text}"
        )

    def allow_keys(self) -> set[str]:
        """The `--allowed-changes` entry shapes that would suppress this."""
        keys = {f"{self.file}::{self.qualname}"}
        if self.old is not None:
            keys.add(f"{self.file}:{self.old.lineno}")
        if self.new is not None:
            keys.add(f"{self.file}:{self.new.lineno}")
        return keys


_ASSERT_ATTR_RE = re.compile(r"^assert")


def _is_assertion_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if _ASSERT_ATTR_RE.match(func.attr):
        return True
    return func.attr == "raises"


def _qualname_of(func_node: ast.AST, class_stack: list[str]) -> str:
    if class_stack:
        return f"{class_stack[-1]}.{func_node.name}"  # type: ignore[union-attr]
    return func_node.name  # type: ignore[union-attr]


def _iter_test_functions(
    tree: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every `def test*`/`async def test*` at module level or one class deep."""
    out: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def walk(node: ast.AST, class_stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, class_stack + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test"):
                    out.append((_qualname_of(child, class_stack), child))
                # Deliberately do not recurse into a test function's own
                # body here -- its nested defs are closures, not separate
                # tests, and are handled by _collect_assertions walking the
                # whole subtree instead.
            else:
                walk(child, class_stack)

    walk(tree, [])
    return out


def _dump_key(node: ast.AST) -> str:
    """`ast.dump()` with every location field stripped -- see module docstring."""
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _collect_assertions(
    source: str, func_node: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str
) -> list[Assertion]:
    found: list[Assertion] = []
    for node in ast.walk(func_node):
        is_assert = isinstance(node, ast.Assert)
        is_assert_call = _is_assertion_call(node)
        if not (is_assert or is_assert_call):
            continue
        text = ast.get_source_segment(source, node) or "<unavailable>"
        found.append(
            Assertion(
                qualname=qualname,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", node.lineno),
                text=text,
                key=_dump_key(node),
            )
        )
    found.sort(key=lambda a: (a.lineno, a.end_lineno))
    return found


def _parse_test_assertions(path: Path) -> dict[str, list[Assertion]]:
    """qualname -> ordered list of assertions, for one file."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise SystemExit(f"error: could not parse {path}: {exc}") from exc
    out: dict[str, list[Assertion]] = {}
    for qualname, func_node in _iter_test_functions(tree):
        out[qualname] = _collect_assertions(source, func_node, qualname)
    return out


def _diff_test_assertions(
    file_rel: str, qualname: str, base: list[Assertion], head: list[Assertion]
) -> list[ChangedAssertion]:
    """Align two ordered assertion lists and report removals/edits.

    Additions (a pure `insert` opcode) are never reported -- see the module
    docstring's "Matching assertions across a diff" section for the full
    reasoning, including how a mixed `replace` block's length difference is
    resolved.
    """
    base_keys = [a.key for a in base]
    head_keys = [a.key for a in head]
    matcher = difflib.SequenceMatcher(None, base_keys, head_keys, autojunk=False)
    changes: list[ChangedAssertion] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            continue  # new assertions added -- allowed
        if tag == "delete":
            for old in base[i1:i2]:
                changes.append(ChangedAssertion(file_rel, qualname, old, None))
            continue
        # tag == "replace"
        old_slice = base[i1:i2]
        new_slice = head[j1:j2]
        for old, new in zip_longest(old_slice, new_slice, fillvalue=None):
            if old is None:
                continue  # extra new item in this block -- treated as an addition
            changes.append(ChangedAssertion(file_rel, qualname, old, new))
    return changes


def _list_test_files(root: Path) -> set[str]:
    """Relative paths (posix-style, relative to `root`) of every `tests/*.py` file."""
    tests_dir = root / "tests"
    out: set[str] = set()
    for path in tests_dir.rglob("*.py"):
        out.add(path.relative_to(root).as_posix())
    return out


def _load_allowed_changes(spec: str | None) -> set[str]:
    """Parse `--allowed-changes` into a flat set of `file:line` / `file::qual` keys.

    Accepts, autodetected by content:
    - a JSON file holding either a list of strings, or an object whose keys
      are the entries (values are free-text reasons, ignored for matching);
    - a plain text / Markdown file: one entry per non-blank, non-`#`-comment
      line, matched via `path/to/file.py:LINE` or `path/to/file.py::qualname`
      -- so a spec's own prose ("retargets tests/test_x.py:42's assertion
      because ...") is directly usable as the allow-list without a separate
      machine-readable copy, as long as each retargeted line/test is
      mentioned in that shape somewhere in the file.
    """
    if not spec:
        return set()
    text = Path(spec).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        return {str(entry).strip() for entry in data}
    if isinstance(data, dict):
        return {str(key).strip() for key in data}

    entries: set[str] = set()
    line_pattern = re.compile(r"([^\s:`'\"]+\.py):(\d+)\b")
    qual_pattern = re.compile(r"([^\s:`'\"]+\.py)::([A-Za-z_][A-Za-z0-9_.]*)")
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for m in line_pattern.finditer(raw_line):
            entries.add(f"{m.group(1)}:{m.group(2)}")
        for m in qual_pattern.finditer(raw_line):
            entries.add(f"{m.group(1)}::{m.group(2)}")
    return entries


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


@dataclass
class Report:
    changes: list[ChangedAssertion] = field(default_factory=list)
    suppressed: list[ChangedAssertion] = field(default_factory=list)
    files_compared: int = 0


def compare_trees(base_root: Path, head_root: Path, allowed: set[str]) -> Report:
    report = Report()
    base_files = _list_test_files(base_root)
    head_files = _list_test_files(head_root)
    common = sorted(base_files & head_files)
    for rel in common:
        base_assertions = _parse_test_assertions(base_root / rel)
        head_assertions = _parse_test_assertions(head_root / rel)
        report.files_compared += 1
        common_qualnames = sorted(set(base_assertions) & set(head_assertions))
        for qualname in common_qualnames:
            for change in _diff_test_assertions(
                rel, qualname, base_assertions[qualname], head_assertions[qualname]
            ):
                if change.allow_keys() & allowed:
                    report.suppressed.append(change)
                else:
                    report.changes.append(change)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base", required=True, help="git ref: the pre-refactor commit"
    )
    parser.add_argument(
        "--head", required=True, help="git ref: the post-refactor commit"
    )
    parser.add_argument(
        "--allowed-changes",
        default=None,
        help=(
            "path to a spec.md / JSON / text file listing file:line or "
            "file::test_function entries this run may change without failing"
        ),
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="skip the worktree checkout and read --base's tests/ from this directory instead",
    )
    parser.add_argument(
        "--head-dir",
        default=None,
        help="skip the worktree checkout and read --head's tests/ from this directory instead",
    )
    args = parser.parse_args(argv)

    allowed = _load_allowed_changes(args.allowed_changes)

    if args.base_dir and args.head_dir:
        report = compare_trees(Path(args.base_dir), Path(args.head_dir), allowed)
        _print_report(report, args)
        return 1 if report.changes else 0

    with tempfile.TemporaryDirectory(prefix="assertions_unchanged_") as tmp:
        tmp_root = Path(tmp)
        base_wt = _worktree(args.base, tmp_root, "base")
        head_wt = _worktree(args.head, tmp_root, "head")
        try:
            report = compare_trees(base_wt, head_wt, allowed)
        finally:
            _remove_worktree(base_wt)
            _remove_worktree(head_wt)

    _print_report(report, args)
    return 1 if report.changes else 0


def _print_report(report: Report, args: argparse.Namespace) -> None:
    print(
        f"assertions_unchanged: compared {report.files_compared} test file(s) "
        f"present at both {args.base} and {args.head}"
    )
    if report.suppressed:
        print(f"  {len(report.suppressed)} change(s) allowed by --allowed-changes:")
        for change in report.suppressed:
            print(f"    {change.file} :: {change.qualname}")
    if not report.changes:
        print("OK: no existing assertion changed.")
        return
    print(
        f"FAIL: {len(report.changes)} assertion(s) changed without being allow-listed:\n"
    )
    for change in report.changes:
        print(change.describe())
        print()


if __name__ == "__main__":
    raise SystemExit(main())
