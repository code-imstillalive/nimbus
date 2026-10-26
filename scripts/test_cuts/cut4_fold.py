"""Fold parametrized tests into one test that loops over the cases (#1837,
the household's ~1,000-test target). v2: fixtures allowed.

- each @pytest.mark.parametrize becomes one `for` (stacked ones nest);
- a `monkeypatch` fixture is scoped per case: the case runs inside
  `with monkeypatch.context() as mp:`, so a case's patches are
  undone before the next;
- a `caplog` fixture is cleared before each case;
- a `return` in the test's own body (not inside a loop of its own) becomes
  `continue`;
- skipped: `indirect`, `pytest.param(...)`, non-literal case lists, async,
  real-HA tests, a `yield`, a nested def/lambda reading a loop variable
  (B023), and files listed on the command line after --skip.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP = set(sys.argv[sys.argv.index("--skip") + 1 :]) if "--skip" in sys.argv else set()
# cut5_merge's autouse rules, read from its source without running its main
_m = (Path(__file__).parent / "cut5_merge.py").read_text(encoding="utf-8")
MERGE = {"__name__": "merge_rules", "__file__": __file__}
exec(compile(_m[: _m.index("\ntotal = 0")], "cut5_merge", "exec"), MERGE)  # noqa: S102


def is_param(dec):
    f = dec.func if isinstance(dec, ast.Call) else None
    return isinstance(f, ast.Attribute) and f.attr == "parametrize"


def names_of(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [n.strip() for n in node.value.split(",") if n.strip()]
    if isinstance(node, (ast.Tuple, ast.List)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
    ):
        return [e.value for e in node.elts]
    return None


def own_nodes(node, in_loop=False):
    """(node, inside-a-loop-of-the-test's-own) for the test's own statements."""
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        yield child, in_loop
        yield from own_nodes(
            child, in_loop or isinstance(child, (ast.For, ast.While, ast.AsyncFor))
        )


def nested_reads(fn, loop_names):
    for n in ast.walk(fn):
        if n is fn:
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for m in ast.walk(n):
                if isinstance(m, ast.Name) and m.id in loop_names:
                    return True
    return False


def is_pytest_param(e):
    return (
        isinstance(e, ast.Call)
        and isinstance(e.func, ast.Attribute)
        and e.func.attr == "param"
        and isinstance(e.func.value, ast.Name)
        and e.func.value.id == "pytest"
    )


def cases_text(node, src):
    """The case list as loop-able source; pytest.param(a, b, id=...) becomes
    (a, b). None when a case carries marks (a skip/xfail would be lost)."""
    seg = ast.get_source_segment(src, node)
    if "pytest.param" not in seg:
        return seg
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    parts = []
    for e in node.elts:
        if is_pytest_param(e):
            if any(k.arg != "id" for k in e.keywords) or not e.args:
                return None
            args = [ast.get_source_segment(src, a) for a in e.args]
            parts.append(args[0] if len(args) == 1 else "(" + ", ".join(args) + ")")
        else:
            s = ast.get_source_segment(src, e)
            if "pytest.param" in s:
                return None
            parts.append(s)
    return "[\n" + "".join(f"    {p},\n" for p in parts) + "]"


def plan(fn, src):
    decs = [d for d in fn.decorator_list if is_param(d)]
    if not decs or isinstance(fn, ast.AsyncFunctionDef):
        return None
    loops = []
    for d in decs:
        if len(d.args) != 2 or any(k.arg != "ids" for k in d.keywords):
            return None
        names = names_of(d.args[0])
        if not names:
            return None
        cases = cases_text(d.args[1], src)
        if cases is None:
            return None
        loops.append((names, cases))
    all_names = [n for names, _ in loops for n in names]
    params = [a.arg for a in fn.args.args]
    if fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
        return None
    if not set(all_names) <= set(params):
        return None
    if nested_reads(fn, set(all_names)):
        return None
    returns = []
    for n, in_loop in own_nodes(fn):
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            return None
        if isinstance(n, ast.Return):
            if in_loop or n.value is not None:
                return None
            returns.append(n)
    keep = [p for p in params if p not in all_names]
    # decorators apply bottom-up; the outermost loop is the top decorator
    return decs, loops, keep, returns


def fold_file(path):
    # the same autouse rules as cut5_merge: a per-test reset the cases
    # would otherwise share is repeated before each case, or the file is left
    rel = path.relative_to(ROOT).as_posix()
    if MERGE["autouse_blocks"](path):
        return 0
    solver = MERGE["SOLVER_CONFTEST"].parent in path.parents
    per_case = ([MERGE["RESET"]] if solver else []) + MERGE["PER_BODY_RESET"].get(
        rel, []
    )
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    edits = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) or not node.name.startswith("test"):
            continue
        if f"{path.relative_to(ROOT).as_posix()}::{node.name}" in SKIP:
            continue
        got = plan(node, src)
        if not got:
            continue
        decs, loops, keep, returns = got
        ind = " " * node.col_offset
        body = node.body
        doc = None
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            doc, body = body[0], body[1:]
        if not body:
            continue
        others = [
            ast.get_source_segment(src, d) for d in node.decorator_list if d not in decs
        ]
        head = "".join(f"{ind}@{d}\n" for d in others)
        head += f"{ind}def {node.name}({', '.join(keep)}):\n"
        if doc:
            head += "".join(lines[doc.lineno - 1 : doc.end_lineno])
        depth = 1
        loop_text = ""
        for names, cases in loops:
            pad = ind + "    " * depth
            cases = cases.replace("\n", "\n" + "    " * (depth - 1))
            loop_text += f"{pad}for {', '.join(names)} in {cases}:\n"
            depth += 1
        pre = ""
        for stmt in per_case:
            pre += f"{ind}{'    ' * depth}{stmt}\n"
        if "caplog" in keep:
            pre += f"{ind}{'    ' * depth}caplog.clear()\n"
        if "monkeypatch" in keep:
            pre += f"{ind}{'    ' * depth}with monkeypatch.context() as mp:\n"
            depth += 1
        first = body[0].lineno - 1
        floor = doc.end_lineno if doc else node.lineno
        while first - 1 >= floor and lines[first - 1].strip().startswith("#"):
            first -= 1
        inner_lines = lines[first : node.end_lineno]
        ret_lines = {r.lineno - 1 - first for r in returns}
        out = []
        for i, l in enumerate(inner_lines):
            if i in ret_lines:
                l = l.replace("return", "continue", 1)
            if "monkeypatch" in keep:
                l = re.sub(r"\bmonkeypatch\b", "mp", l)
            out.append(("    " * (depth - 1) + l) if l.strip() else l)
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        edits.append((start, node.end_lineno, head + loop_text + pre + "".join(out)))
    if not edits:
        return 0
    for start, end, text in sorted(edits, reverse=True):
        lines[start - 1 : end] = [text]
    new = "".join(lines)
    if solver and "from solver import lp as _solver_lp" not in new:
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
    path.write_text(new, encoding="utf-8", newline="\n")
    return len(edits)


total = 0
for p in sorted((ROOT / "tests").rglob("test_*.py")):
    rel = p.relative_to(ROOT).as_posix()
    if "hass_integration" in p.parts or rel in SKIP:
        continue
    try:
        total += fold_file(p)
    except SyntaxError as e:
        print("SKIP", rel, e, file=sys.stderr)
print("folded functions:", total)
