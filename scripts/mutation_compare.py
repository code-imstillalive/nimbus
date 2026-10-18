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


def _mutate(source: str, index: int) -> tuple[str, str]:
    tree = ast.parse(source)
    node = list(ast.walk(tree))[index]
    line = getattr(node, "lineno", 0)
    if isinstance(node, ast.Compare):
        before = type(node.ops[0]).__name__
        node.ops[0] = SWAPS[type(node.ops[0])]()
        what = f"{before} -> {type(node.ops[0]).__name__}"
    elif isinstance(node, (ast.BinOp, ast.BoolOp)):
        before = type(node.op).__name__
        node.op = SWAPS[type(node.op)]()
        what = f"{before} -> {type(node.op).__name__}"
    else:
        before = node.value
        node.value = before + 1 if before == 0 else before * 2
        what = f"{before!r} -> {node.value!r}"
    return ast.unparse(tree), f"line {line}: {what}"


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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
    rows = []
    try:
        for n, (index, kind) in enumerate(chosen, 1):
            mutated, what = _mutate(original, index)
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
