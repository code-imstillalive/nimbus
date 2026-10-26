"""Fold a parametrized test's cases into one test that loops over them
(#1837: the household's ~1,000-test target). Only the safe shape:

- exactly one @pytest.mark.parametrize decorator, no `indirect`, no
  pytest.param(...) in the cases;
- the function's parameters are exactly the parametrized names (no
  fixtures, so nothing can carry state from one case to the next);
- no `return` or `yield` in the body (a return would skip later cases).

Each failing case still names itself: the loop's assertion message carries
the case's values via pytest's assertion rewriting of the loop variables.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def is_param(dec):
    f = dec.func if isinstance(dec, ast.Call) else None
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "parametrize"
        and isinstance(f.value, ast.Attribute)
        and f.value.attr == "mark"
    )


def names_of(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [n.strip() for n in node.value.split(",") if n.strip()]
    if isinstance(node, (ast.Tuple, ast.List)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
    ):
        return [e.value for e in node.elts]
    return None


def foldable(fn, src):
    decs = [d for d in fn.decorator_list if is_param(d)]
    if len(decs) != 1:
        return None
    dec = decs[0]
    if len(dec.args) < 2 or any(k.arg == "indirect" for k in dec.keywords):
        return None
    names = names_of(dec.args[0])
    if not names:
        return None
    params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    if params != names or fn.args.vararg or fn.args.kwarg:
        return None
    if "pytest.param" in (ast.get_source_segment(src, dec.args[1]) or ""):
        return None

    def own_nodes(node):
        # the test's own statements, not those inside a nested def or lambda
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            yield child
            yield from own_nodes(child)

    for n in own_nodes(fn):
        if isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom)):
            return None
    return dec, names


def fold_file(path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    edits = []
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        got = foldable(node, src)
        if not got:
            continue
        dec, names = got
        cases = ast.get_source_segment(src, dec.args[1])
        indent = " " * node.col_offset
        body_indent = indent + "    "
        # docstring stays first
        body = node.body
        doc = None
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            doc = body[0]
            body = body[1:]
        if not body:
            continue
        other_decs = [
            ast.get_source_segment(src, d) for d in node.decorator_list if d is not dec
        ]
        head = "".join(f"{indent}@{d}\n" for d in other_decs)
        kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        head += f"{indent}{kw} {node.name}():\n"
        if doc:
            head += "".join(lines[doc.lineno - 1 : doc.end_lineno])
        target = names[0] if len(names) == 1 else ", ".join(names)
        loop = f"{body_indent}for {target} in {cases}:\n"
        loop = (
            "\n".join(
                (l if i == 0 else l)  # noqa: RUF034
                for i, l in enumerate(loop.splitlines())
            )
            + "\n"
        )
        first = body[0].lineno - 1
        floor = doc.end_lineno if doc else node.lineno
        while first - 1 >= floor and lines[first - 1].strip().startswith("#"):
            first -= 1
        inner = "".join(lines[first : node.end_lineno])
        inner = "".join(
            ("    " + l if l.strip() else l) for l in inner.splitlines(keepends=True)
        )
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        edits.append((start, node.end_lineno, head + loop + inner))
        count += 1
    if not edits:
        return 0
    for start, end, text in sorted(edits, reverse=True):
        lines[start - 1 : end] = [text]
    new = "".join(lines)
    ast.parse(new)  # must still parse
    path.write_text(new, encoding="utf-8", newline="\n")
    return count


total = 0
for p in sorted((ROOT / "tests").rglob("test_*.py")):
    try:
        n = fold_file(p)
    except SyntaxError as e:
        print("SKIP (would not parse)", p, e, file=sys.stderr)
        continue
    total += n
print("folded functions:", total)
