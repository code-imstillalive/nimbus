"""Merge runs of consecutive tests into one test (#1837: the household's
~1,000-test target). v2: tests taking fixtures merge too.

The merged test takes every fixture its members took. Per member body:
- `monkeypatch` -> runs inside `with monkeypatch.context() as mp:`, so its
  patches are undone before the next body;
- `caplog` -> cleared first;
- `tmp_path` -> its own subdirectory;
- under tests/v2/solver, the conftest's cooldown reset is repeated first.
Other fixtures are shared by the merged bodies; CI shows any that carry
state, and those files are reverted.

Skipped: async, decorated, `return`/`yield`, a class with setup/teardown or
a decorator, files with their own autouse fixture (or a conftest one other
than v2/solver's), real-HA tests, and the files after --skip.

    python scripts/test_cuts/cut5_merge.py [--dry-run] [--chunk=N] [--skip f1 f2 ...]
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHUNK = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--chunk=")), "8"))
DRY = "--dry-run" in sys.argv
SKIP = set(sys.argv[sys.argv.index("--skip") + 1 :]) if "--skip" in sys.argv else set()
SETUP = {
    "setup_method",
    "teardown_method",
    "setup_class",
    "teardown_class",
    "setup",
    "teardown",
}
SOLVER_CONFTEST = ROOT / "tests" / "v2" / "solver" / "conftest.py"
RESET = "_solver_lp._lex_calibration_failed_until = 0.0"
# autouse fixtures that only monkeypatch module attributes for the test: a
# merged test gets the same patches, and each body's own patches are scoped
PATCH_ONLY_AUTOUSE = {
    "tests/v2/entities/test_services.py",
    "tests/v2/setup/test_subentry_wizards.py",
    # these two only reset the cooldown, which RESET repeats per body
    "tests/v2/solver/test_lp_failure_paths.py",
    "tests/v2/solver/test_lp_paths.py",
}
# autouse fixtures that reset module state: their reset, repeated per body
PER_BODY_RESET = {
    "tests/v2/solver/test_stochastic_orchestration.py": [
        "assert solver_plan.wait_for_stochastic_shadow(timeout=60)",
        "solver_plan.reset_stochastic_state()",
    ],
    "tests/v2/solver/test_stochastic_visibility.py": [
        "solver_plan.reset_stochastic_state()"
    ],
    "tests/v2/reports/test_flex_telemetry_record.py": [
        'solver_writer.publish_flex_telemetry_record.__globals__["_LAST_FLEX_TELEMETRY_POST"].clear()',
        'solver_writer.publish_flex_telemetry_record.__globals__["_LAST_FLEX_TELEMETRY_NO_RECORD"].clear()',
    ],
}


def own_nodes(node):
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        yield child
        yield from own_nodes(child)


def fixtures(fn, in_class):
    a = [x.arg for x in fn.args.args]
    return a[1:] if in_class else a


CURRENT = [""]


def qualifies(fn, in_class):
    if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test"):
        return False
    if f"{CURRENT[0]}::{fn.name}" in SKIP:
        return False
    a = fn.args
    if in_class and (not a.args or a.args[0].arg != "self"):
        return False
    if (
        a.kwonlyargs
        or a.vararg
        or a.kwarg
        or a.posonlyargs
        or a.defaults
        or fn.decorator_list
    ):
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


def independent(run, fn, in_class):
    fx = set(fixtures(fn, in_class))
    for other in run:
        ofx = set(fixtures(other, in_class))
        shared = names(other, ast.Store) & (names(fn, ast.Load) - names(fn, ast.Store))
        back = names(fn, ast.Store) & (names(other, ast.Load) - names(other, ast.Store))
        # a body may not rebind a fixture name another body reads
        clash = (names(other, ast.Store) & fx) | (names(fn, ast.Store) & ofx)
        if shared or back or clash:
            return False
    return True


def collect(body_nodes, in_class):
    runs, cur = [], []
    for node in body_nodes:
        if qualifies(node, in_class):
            if cur and not independent(cur, node, in_class):
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


def merged_text(run, lines, in_class, solver):
    ind = " " * run[0].col_offset
    allfx = []
    for fn in run:
        for f in fixtures(fn, in_class):
            if f not in allfx:
                allfx.append(f)
    parts = []
    for i, fn in enumerate(run):
        fx = fixtures(fn, in_class)
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
        b = ind + "    "
        head = f"{b}# {fn.name}" + (f": {doc}" if doc else "") + "\n"
        if solver:
            head += f"{b}{RESET}\n"
        for stmt in PER_BODY_RESET.get(CURRENT[0], []):
            head += f"{b}{stmt}\n"
        if "caplog" in fx:
            head += f"{b}caplog.clear()\n"
        text = "".join(lines[first : fn.end_lineno])
        extra = ""
        if "tmp_path" in fx:
            tp = f"tmp_{i}"
            head += f"{b}{tp} = tmp_path / {fn.name!r}\n{b}{tp}.mkdir()\n"
            text = re.sub(r"\btmp_path\b", tp, text)
        if "monkeypatch" in fx:
            head += f"{b}with monkeypatch.context() as mp:\n"
            text = re.sub(r"\bmonkeypatch\b", "mp", text)
            extra = "    "
        text = "".join(
            (extra + l) if l.strip() else l for l in text.splitlines(keepends=True)
        )
        parts.append(head + text)
    args = (["self"] if in_class else []) + allfx
    return f"{ind}def {run[0].name}({', '.join(args)}):\n" + "\n".join(parts)


def autouse_blocks(path):
    for f in [
        path,
        *[d / "conftest.py" for d in path.parents if ROOT in d.parents or d == ROOT],
    ]:
        rel = f.relative_to(ROOT).as_posix()
        if f == SOLVER_CONFTEST or rel in PATCH_ONLY_AUTOUSE or rel in PER_BODY_RESET:
            continue
        if f.exists() and "autouse=True" in f.read_text(encoding="utf-8"):
            return True
    return False


def merge_file(path):
    CURRENT[0] = path.relative_to(ROOT).as_posix()
    if autouse_blocks(path):
        return 0
    solver = SOLVER_CONFTEST.parent in path.parents
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    has_import = "from solver import lp as _solver_lp" in src
    lines = src.splitlines(keepends=True)
    runs = [(r, False) for r in collect(tree.body, False)]
    for node in tree.body:
        if (
            isinstance(node, ast.ClassDef)
            and node.name.startswith("Test")
            and not node.decorator_list
            and not any(
                isinstance(m, ast.FunctionDef) and m.name in SETUP for m in node.body
            )
        ):
            runs += [(r, True) for r in collect(node.body, True)]
    if not runs:
        return 0
    spans = []
    for run, in_class in runs:
        spans.append(
            (
                run[0].lineno,
                run[0].end_lineno,
                merged_text(run, lines, in_class, solver),
            )
        )
        spans += [(fn.lineno, fn.end_lineno, "") for fn in run[1:]]
    for start, end, text in sorted(spans, key=lambda s: s[0], reverse=True):
        lines[start - 1 : end] = [text]
    new = "".join(lines)
    if solver and not has_import:
        # after the module docstring and `from __future__`, before the rest
        t = ast.parse(new)
        at = 0
        for n in t.body:
            if (
                isinstance(n, ast.Expr)
                and isinstance(getattr(n, "value", None), ast.Constant)
                or isinstance(n, ast.ImportFrom)
                and n.module == "__future__"
            ):
                at = n.end_lineno
            else:
                break
        nl = new.splitlines(keepends=True)
        nl[at:at] = ["from solver import lp as _solver_lp\n"]
        new = "".join(nl)
    ast.parse(new)
    if not DRY:
        path.write_text(new, encoding="utf-8", newline="\n")
    return sum(len(r) - 1 for r, _ in runs)


total = 0
for p in sorted((ROOT / "tests").rglob("test_*.py")):
    rel = p.relative_to(ROOT).as_posix()
    if "hass_integration" in p.parts or rel in SKIP:
        continue
    total += merge_file(p)
print("tests removed by merging:", total)
