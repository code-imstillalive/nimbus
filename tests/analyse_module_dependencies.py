"""What would it cost to extract these functions from a module?

Built for #1298's decomposition of `solver_writer.py` and generalised so every
phase of it can be measured rather than estimated. Run it against the current
tree before planning a move, and again after, to see whether the move actually
reduced coupling.

## The question it answers

Phase 1 (#1308) was a clean extraction because `solver_inputs/extra_batteries.py`
imports only `..solver` and stdlib -- its dependencies were already in the pure,
HA-import-free package. That is not true of every candidate. A function that
reads names defined at `solver_writer`'s own HA/recorder level cannot be moved
into a submodule that `solver_writer` imports, because the submodule would have
to import back and close a cycle.

So for each target function this reports its module-scope reads, split into:

* **importable** -- names `solver_writer` itself imports, so an extracted module
  can import them from the same place. Free.
* **module-level (the blocker)** -- names defined IN `solver_writer`. Each one
  must be moved first, injected as a parameter, or late-imported. This count is
  the real cost of the extraction.

It then aggregates the blockers by how many targets need them, which is what
identifies the shared core that has to move before anything else can.

## Why AST and not imports

It parses the file; it never imports it. That keeps it runnable with no HA
present, no fixtures, no recorder, and no risk of executing module-level code --
and it is why the numbers are reproducible on any checkout, including from CI or
from a bisect.

The trade is that it measures *static* reads. A name reached only through
`getattr` or a late `import` inside a branch will not appear. For this codebase
that is the right trade: the coupling that blocks an extraction is exactly the
coupling that is visible statically.

## Usage

    python tests/analyse_module_dependencies.py                    # Phase 2 set
    python tests/analyse_module_dependencies.py --phase 3
    python tests/analyse_module_dependencies.py --functions a,b,c
    python tests/analyse_module_dependencies.py --module custom_components/nimbus_load/sensor.py --functions x

Exit status is always 0: this is a measurement tool, not a gate. Nothing here
asserts a threshold, because "how coupled is too coupled" is a judgement for the
phase's own issue, not something to hardcode.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_MODULE = _REPO / "custom_components" / "nimbus_load" / "solver_writer.py"

#: The function sets each #1298 phase proposes to move, so a phase can be
#: measured by number instead of retyping eight names. Taken from the phase
#: issues themselves; update alongside them rather than guessing.
PHASES: dict[str, tuple[str, ...]] = {
    "2": (
        "_compute_report_for_window",
        "publish_daily_quality_report",
        "rescore_quality_history",
        "_carry_forward_quality_history",
        "_soc_discrepancy_stats",
        "compute_nimbus_only_soc_counterfactual",
        "compute_efficiency_backtest_report",
        "_compute_flex_report_for_window",
    ),
    "3": ("build_controllable_loads",),
    "5": ("publish_plan",),
    "6": ("apply_commanded_state_guard",),
}


def _module_level_names(tree: ast.Module) -> set[str]:
    """Names bound at module scope -- the potential blockers."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_names(tree: ast.Module) -> set[str]:
    """Every name this module imports, at any nesting level.

    Includes imports inside `try`/`except ImportError` blocks, because this
    codebase uses that pattern deliberately for the standalone/cron path and
    those names are just as importable from an extracted module.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _bound_locally(fn: ast.AST) -> set[str]:
    """Everything the function binds itself: arguments, assignments, loop
    targets, comprehension targets, nested defs, `except ... as`, and its own
    local imports."""
    bound: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) or (isinstance(node, ast.ExceptHandler) and node.name):
            # Both carry the bound name on `.name`: a nested def/class, and an
            # `except ... as err`.
            bound.add(node.name)
        elif isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.Global):
            # A `global x` still reads module state -- deliberately NOT treated
            # as locally bound, because that is exactly the coupling this tool
            # exists to surface.
            bound -= set(node.names)
    return bound


def _external_reads(fn: ast.AST) -> set[str]:
    """Names the function reads but does not bind, builtins excluded.

    An attribute chain is reduced to its root (`np.array` -> `np`), because the
    root is what an extracted module would have to import.
    """
    used: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            root: ast.AST = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
    return {
        name
        for name in used - _bound_locally(fn)
        if not hasattr(builtins, name) and not name.startswith("__")
    }


def _size(fn: ast.AST) -> int:
    end = getattr(fn, "end_lineno", None) or getattr(fn, "lineno", 0)
    return end - getattr(fn, "lineno", 0) + 1


def analyse(module_path: Path, targets: tuple[str, ...]) -> int:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    module_level = _module_level_names(tree)
    imported = _imported_names(tree)

    blockers: dict[str, list[str]] = {}
    total_lines = 0
    missing: list[str] = []

    for name in targets:
        fn = functions.get(name)
        if fn is None:
            missing.append(name)
            continue
        external = _external_reads(fn)
        free = sorted(n for n in external if n in imported)
        blocked = sorted(n for n in external if n in module_level and n not in imported)
        unresolved = sorted(
            n for n in external if n not in imported and n not in module_level
        )
        total_lines += _size(fn)
        for dep in blocked:
            blockers.setdefault(dep, []).append(name)

        print(f"### {name}  ({_size(fn)} lines)")
        print(f"    importable:              {len(free)}")
        print(f"    module-level (blockers): {len(blocked)}")
        if blocked:
            print("      " + ", ".join(blocked))
        if unresolved:
            # Usually a builtin this tool does not know about, or a name bound
            # by a construct not modelled above. Printed rather than hidden so
            # the count above can be trusted.
            print(f"    unresolved: {', '.join(unresolved)}")
        print()

    if missing:
        print(f"!! not found in {module_path.name}: {', '.join(missing)}\n")

    print("=" * 70)
    print("Shared blockers, by how many targets need them")
    print("=" * 70)
    for dep, users in sorted(blockers.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        fn = functions.get(dep)
        size = f"  [{_size(fn)} lines]" if fn is not None else ""
        print(f"{len(users)}x  {dep}{size}")
        if len(users) >= 3:
            print(f"      {', '.join(users)}")

    print()
    print(f"lines in scope:      {total_lines}")
    print(f"distinct blockers:   {len(blockers)}")
    print()
    print(
        "A blocker shared by several targets is a shared-core candidate: moving "
        "it first makes every target that needs it cheaper. A target with a low "
        "blocker count is the natural first move."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--module",
        type=Path,
        default=_DEFAULT_MODULE,
        help="module to analyse (default: solver_writer.py)",
    )
    parser.add_argument(
        "--phase",
        choices=sorted(PHASES),
        default="2",
        help="a #1298 phase's own function set (default: 2)",
    )
    parser.add_argument(
        "--functions",
        help="comma-separated function names, instead of a phase set",
    )
    args = parser.parse_args(argv)

    if args.functions:
        targets = tuple(n.strip() for n in args.functions.split(",") if n.strip())
    else:
        targets = PHASES[args.phase]

    if not args.module.exists():
        print(f"no such module: {args.module}", file=sys.stderr)
        return 2
    return analyse(args.module, targets)


if __name__ == "__main__":
    raise SystemExit(main())
