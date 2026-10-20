#!/usr/bin/env python3
"""Plant bugs in one module and count which test set catches each (#1837).

Coverage shows which lines a suite runs, not whether it checks them. This
plants single-operator mutants (comparison, arithmetic, boolean, numeric
constant) at random in the chosen functions of one source file, then runs
each test set against every mutant. A mutant is killed when the set fails.

    python scripts/mutation_compare.py \\
        --module custom_components/nimbus_load/solver/network.py \\
        --function _build_plan_once \\
        --set old=@oldsolver.txt --set new=tests/v2/solver \\
        --mutants 40 --seed 1837 --json out.json

Run it in a scratch worktree: it rewrites the module in place and restores
it after each mutant. Mutants that compile to the same bytecode, or that no
set's tests ever reach, still count; both sets face the same ones.
"""

from __future__ import annotations

import argparse
import ast
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

SWAPS = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.And: ast.Or,
    ast.Or: ast.And,
}


def _sites(tree: ast.AST, functions: set[str]) -> list[tuple[int, str]]:
    """(index into ast.walk order, kind) for every mutable node."""
    out = []
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            not functions or node.name in functions
        ):
            inside.update(id(n) for n in ast.walk(node))
    for i, node in enumerate(ast.walk(tree)):
        if id(node) not in inside:
            continue
        if isinstance(node, ast.Compare) and type(node.ops[0]) in SWAPS:
            out.append((i, "compare"))
        elif isinstance(node, ast.BinOp) and type(node.op) in SWAPS:
            out.append((i, "binop"))
        elif isinstance(node, ast.BoolOp) and type(node.op) in SWAPS:
            out.append((i, "boolop"))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
        ):
            out.append((i, "constant"))
    return out


#: Each operator's source text, and a pattern that finds exactly it.
_TOKENS = {
    ast.Lt: ("<", r"(?<![<>=!])<(?![<=])"),
    ast.LtE: ("<=", r"<="),
    ast.Gt: (">", r"(?<![<>=\-])>(?![>=])"),
    ast.GtE: (">=", r">="),
    ast.Eq: ("==", r"=="),
    ast.NotEq: ("!=", r"!="),
    ast.Add: ("+", r"(?<![+=*])\+(?!=)"),
    ast.Sub: ("-", r"(?<![\-=])-(?![=>])"),
    ast.Mult: ("*", r"(?<!\*)\*(?![*=])"),
    ast.Div: ("/", r"(?<!/)/(?![/=])"),
    ast.And: ("and", r"\band\b"),
    ast.Or: ("or", r"\bor\b"),
}


def _offset(lines: list[bytes], lineno: int, col: int) -> int:
    """A byte offset into the file from an AST position (cols are UTF-8
    byte offsets)."""
    return sum(len(x) for x in lines[: lineno - 1]) + col


def _span(lines: list[bytes], node: ast.AST, end: bool) -> int:
    if end:
        return _offset(lines, node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]
    return _offset(lines, node.lineno, node.col_offset)  # type: ignore[attr-defined]


def _replace_op(
    data: bytes, lines: list[bytes], left: ast.AST, right: ast.AST, op: type
) -> bytes:
    """Swap the one operator token between `left` and `right` in place."""
    lo, hi = _span(lines, left, True), _span(lines, right, False)
    gap = data[lo:hi].decode("utf-8")
    # Blank out comments (same length) so a `#` note can't hold the token.
    masked = re.sub(r"#[^\n]*", lambda m: " " * len(m.group(0)), gap)
    found = list(re.finditer(_TOKENS[op][1], masked))
    if len(found) != 1:
        raise ValueError(f"no single {_TOKENS[op][0]!r} between operands")
    new = _TOKENS[SWAPS[op]][0]
    m = found[0]
    gap = gap[: m.start()] + new + gap[m.end() :]
    return data[:lo] + gap.encode("utf-8") + data[hi:]


def _mutate(source: str, index: int) -> tuple[str, str]:
    """`source` with one site mutated by replacing only that operator's or
    constant's own characters. Everything else, comments and formatting
    included, is untouched: a test that reads the module's text (#1837, 20 Oct
    2026) must not fail on a reformatted file and count as a kill."""
    tree = ast.parse(source)
    node = list(ast.walk(tree))[index]
    line = getattr(node, "lineno", 0)
    data = source.encode("utf-8")
    lines = data.splitlines(keepends=True)
    if isinstance(node, ast.Compare):
        op = type(node.ops[0])
        data = _replace_op(data, lines, node.left, node.comparators[0], op)
        what = f"{op.__name__} -> {SWAPS[op].__name__}"
    elif isinstance(node, ast.BinOp):
        op = type(node.op)
        data = _replace_op(data, lines, node.left, node.right, op)
        what = f"{op.__name__} -> {SWAPS[op].__name__}"
    elif isinstance(node, ast.BoolOp):
        op = type(node.op)
        # Every joint of the chain, last first so earlier offsets hold.
        for left, right in reversed(list(zip(node.values, node.values[1:]))):
            data = _replace_op(data, lines, left, right, op)
        what = f"{op.__name__} -> {SWAPS[op].__name__}"
    else:
        before = node.value
        after = before + 1 if before == 0 else before * 2
        lo, hi = _span(lines, node, False), _span(lines, node, True)
        data = data[:lo] + repr(after).encode("utf-8") + data[hi:]
        what = f"{before!r} -> {after!r}"
    mutated = data.decode("utf-8")
    ast.parse(mutated)  # still valid Python, or this site is skipped
    return mutated, f"line {line}: {what}"


def _run(target: str, timeout: int) -> str:
    args = target[1:].split() if target.startswith("@") else [target]
    if target.startswith("@"):
        args = Path(target[1:]).read_text(encoding="utf-8").split()
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:homeassistant",
        "-x",
        "-q",
        "-n",
        "8",
        "-p",
        "no:cacheprovider",
        *args,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return "killed (timeout)"
    return "survived" if r.returncode == 0 else "killed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", required=True)
    ap.add_argument("--function", action="append", default=[])
    ap.add_argument("--set", action="append", required=True, help="name=target")
    ap.add_argument("--mutants", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1837)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--json")
    a = ap.parse_args()

    path = Path(a.module)
    original = path.read_text(encoding="utf-8")
    sites = _sites(ast.parse(original), set(a.function))
    rng = random.Random(a.seed)
    chosen = rng.sample(sites, min(a.mutants, len(sites)))
    sets = dict(s.split("=", 1) for s in a.set)
    # A set that fails on the unmutated module would "kill" every mutant.
    for name, target in sets.items():
        if _run(target, a.timeout) != "survived":
            print(json.dumps({"error": f"set {name!r} fails on the unmutated code"}))
            return 2
    rows = []
    try:
        for n, (index, kind) in enumerate(chosen, 1):
            try:
                mutated, what = _mutate(original, index)
            except (ValueError, SyntaxError) as err:
                print(json.dumps({"mutant": n, "skipped": str(err)}), flush=True)
                continue
            path.write_text(mutated, encoding="utf-8")
            row = {"mutant": n, "kind": kind, "where": what}
            for name, target in sets.items():
                t0 = time.monotonic()
                row[name] = _run(target, a.timeout)
                row[name + "_s"] = round(time.monotonic() - t0, 1)
            rows.append(row)
            print(json.dumps(row), flush=True)
    finally:
        path.write_text(original, encoding="utf-8")

    summary = {"module": a.module, "sites": len(sites), "mutants": len(rows)}
    for name in sets:
        summary[name + "_killed"] = sum(r[name].startswith("killed") for r in rows)
    print(json.dumps(summary))
    if a.json:
        Path(a.json).write_text(
            json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
