"""Delete the test functions the cut list says reach nothing the kept ones
don't (#1837). Removes each function with its decorators; deletes a file
left with no test; prints counts."""

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
cover = json.load(open(sys.argv[1], encoding="utf-8"))  # noqa: SIM115
drop = defaultdict(set)
for node in cover["adds_nothing"]:
    path, rest = node.split("::", 1)
    drop[path].add(tuple(rest.split("::")))

removed_funcs = 0
deleted_files = []
missing = []
for path, names in sorted(drop.items()):
    f = ROOT / path
    if not f.exists():
        missing.append(path)
        continue
    src = f.read_text(encoding="utf-8")
    tree = ast.parse(src)
    spans = []
    found = set()

    def visit(body, prefix):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (*prefix, n.name)
                if key in names:  # noqa: B023
                    start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                    spans.append((start, n.end_lineno))  # noqa: B023
                    found.add(key)  # noqa: B023
            elif isinstance(n, ast.ClassDef):
                before = len(spans)  # noqa: B023
                visit(n.body, (*prefix, n.name))
                gone = len(spans) - before  # noqa: B023
                body = [
                    m
                    for m in n.body
                    if not (
                        isinstance(m, ast.Expr)
                        and isinstance(getattr(m, "value", None), ast.Constant)
                    )
                ]
                if gone and gone == len(body):
                    # every member removed: drop the whole class instead
                    del spans[before:]  # noqa: B023
                    start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                    spans.append((start, n.end_lineno))  # noqa: B023

    visit(tree.body, ())
    for k in names - found:
        missing.append(f"{path}::{'::'.join(k)}")
    if not spans:
        continue
    lines = src.splitlines(keepends=True)
    for start, end in sorted(spans, reverse=True):
        del lines[start - 1 : end]
    new = "".join(lines)
    removed_funcs += len(spans)
    rest = ast.parse(new)
    has_test = any(
        (
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")
        )
        or (
            isinstance(n, ast.ClassDef)
            and any(
                isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and m.name.startswith("test")
                for m in n.body
            )
        )
        for n in ast.walk(rest)
    )
    imported = any(
        f"import {f.stem}" in t.read_text(encoding="utf-8", errors="ignore")
        or f"from {f.stem} import" in t.read_text(encoding="utf-8", errors="ignore")
        for t in (ROOT / "tests").rglob("*.py")
        if t != f
    )
    if not has_test and not imported:
        f.unlink()
        deleted_files.append(path)
    else:
        f.write_text(new, encoding="utf-8", newline="\n")

print(
    json.dumps(
        {
            "functions_removed": removed_funcs,
            "files_deleted": deleted_files,
            "not_found": missing,
        },
        indent=1,
    )
)
