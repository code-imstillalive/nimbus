"""Merge runs of consecutive setup-free tests into one test (#1837: the
household's ~1,000-test target).

A test qualifies when it is `def test_*()` at module level, or
`def test_*(self)` in a `Test*` class with no setup/teardown and no class
decorator, with no decorators of its own and no `return`/`yield` of its
own (a return would skip the tests merged after it). Consecutive
qualifying tests are merged, up to --chunk per test, in file order. Each
original body is kept whole under a comment naming it.

    python scripts/test_cuts/cut3_merge.py [--dry-run] [--chunk=N]
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHUNK = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--chunk=")), "6"))
DRY = "--dry-run" in sys.argv
SETUP = {
    "setup_method",
    "teardown_method",
    "setup_class",
    "teardown_class",
    "setup",
    "teardown",
}


def own_nodes(node):
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        yield child
        yield from own_nodes(child)


def qualifies(fn, in_class):
    if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test"):
        return False
    a = fn.args
    if [x.arg for x in a.args] != (["self"] if in_class else []):
        return False
    if a.kwonlyargs or a.vararg or a.kwarg or a.posonlyargs or fn.decorator_list:
        return False
    return not any(
        isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom)) for n in own_nodes(fn)
    )


def names(fn, ctx):
    return {
        n.id
        for n in own_nodes(fn)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ctx)
    }


def independent(run, fn):
    """No member binds a name another member reads without binding it
    itself: a binding in one body can't change what another body sees."""
    for other in run:
        shared = names(other, ast.Store) & (names(fn, ast.Load) - names(fn, ast.Store))
        back = names(fn, ast.Store) & (names(other, ast.Load) - names(other, ast.Store))
        if shared or back:
            return False
    return True


def collect(body_nodes, in_class):
    runs, cur = [], []
    for node in body_nodes:
        if qualifies(node, in_class):
            if cur and not independent(cur, node):
                if len(cur) > 1:
                    runs.append(cur)
                cur = []
            cur.append(node)
            if len(cur) == CHUNK:
                runs.append(cur)
                cur = []
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and (node.name.startswith(("test", "Test"))):
            if len(cur) > 1:
                runs.append(cur)
            cur = []
    if len(cur) > 1:
        runs.append(cur)
    return [r for r in runs if len(r) > 1]


def merged_text(run, lines):
    ind = " " * run[0].col_offset
    parts = []
    for fn in run:
        body = fn.body
        doc = None
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            doc = (
                body[0].value.value.strip().splitlines()[0]
                if body[0].value.value.strip()
                else ""
            )
            body = body[1:]
        if not body:
            continue
        first = body[0].lineno - 1
        floor = fn.body[0].end_lineno if doc is not None else fn.lineno
        while first - 1 >= floor and lines[first - 1].strip().startswith("#"):
            first -= 1
        head = f"{ind}    # {fn.name}" + (f": {doc}" if doc else "") + "\n"
        parts.append(head + "".join(lines[first : fn.end_lineno]))
    args = "self" if run[0].args.args else ""
    return f"{ind}def {run[0].name}({args}):\n" + "\n".join(parts)


def has_autouse(path):
    # a per-test reset (autouse fixture) in the file or a conftest above it
    for f in [
        path,
        *[d / "conftest.py" for d in path.parents if ROOT in d.parents or d == ROOT],
    ]:
        if f.exists() and "autouse=True" in f.read_text(encoding="utf-8"):
            return True
    return False


def merge_file(path):
    if has_autouse(path):
        return 0
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    runs = collect(tree.body, False)
    for node in tree.body:
        if (
            isinstance(node, ast.ClassDef)
            and node.name.startswith("Test")
            and not node.decorator_list
            and not any(
                isinstance(m, ast.FunctionDef) and m.name in SETUP for m in node.body
            )
        ):
            runs += collect(node.body, True)
    if not runs:
        return 0
    spans = []
    for run in runs:
        spans.append((run[0].lineno, run[0].end_lineno, merged_text(run, lines)))
        spans += [(fn.lineno, fn.end_lineno, "") for fn in run[1:]]
    for start, end, text in sorted(spans, key=lambda s: s[0], reverse=True):
        lines[start - 1 : end] = [text]
    new = "".join(lines)
    ast.parse(new)
    if not DRY:
        path.write_text(new, encoding="utf-8", newline="\n")
    return sum(len(r) - 1 for r in runs)


total = 0
for p in sorted((ROOT / "tests").rglob("test_*.py")):
    if "hass_integration" in p.parts:
        continue  # real-HA tests always take the `hass` fixture
    total += merge_file(p)
print("tests removed by merging:", total)
