"""The Solver writer's source text, as ONE searchable string across every
module it has been decomposed into.

## Why this exists

A large family of tests in this directory assert that something is *wired
in* rather than merely defined -- that a call site exists, that a warning is
raised inside a conditional, that a published field reaches the payload.
They do it by reading `solver_writer.py`'s source and searching for a
literal. That is a genuinely good technique: it catches "the helper exists
but nothing calls it", which no amount of unit-testing the helper can.

It has one failure mode, and #1298's decomposition triggers it on every
phase. When a function moves out of `solver_writer.py` into
`solver_reports/`, `solver_inputs/`, `solver_shared.py`,
`solver_publish.py` or `solver_plan.py`, the literal those tests search for
moves with it. The
behaviour is completely unchanged -- the facade re-exports every name, and
the call sites still run -- but a test reading one file finds nothing and
fails. Measured on #1301 Phase 2b+2c: **32 tests across 7 files**, none of
them describing a real regression.

Patching those 32 assertions one phase at a time would mean doing it again
for Phases 3, 4, 5, 6 and 7. So the fix is to make "the writer's source"
mean what it already means everywhere else in this repo: the union.

`test_docs_writer_function_set_drift.py` reached this conclusion first, for
the same reason, and states it plainly in its own docstring -- *"the
integration side is a UNION across solver_writer.py and every module #735
has extracted out of it"*. This module is that idea applied to source-text
searches instead of function-name sets.

## What it does NOT do

It does not make a search less strict. A literal still has to appear
somewhere in the real shipped integration code. What it stops asserting is
*which file* the literal lives in -- which was never the property those
tests were written to protect, and is exactly the property a pure
relocation is allowed to change.

If a test genuinely needs to pin a literal to one specific module, read
that module directly and say why in the test. `inspect.getsource()` on the
function itself is usually the better tool for that, and it already follows
the facade's re-export aliases to wherever the function now lives.
"""

from __future__ import annotations

import ast
import pathlib

import _solver_path  # noqa: F401 -- sys.path setup side effect
import solver_writer

_NIMBUS_DIR = pathlib.Path(solver_writer.__file__.replace(".pyc", ".py")).parent

# Every module #1298 has extracted out of solver_writer.py so far, plus the
# file itself. Kept as an explicit list rather than a recursive glob of the
# package: `sensor.py`, `config_flow.py` and the other HA platform modules are
# NOT the writer, and folding them in would let a literal "pass" from a file
# the test never meant to search.
_EXTRACTED = (
    "solver_shared.py",
    "solver_publish.py",
    "solver_plan.py",  # nimbus #1303 (spec 004, Phase 4)
    "solver_dispatch/*.py",  # nimbus #1305 (spec 006, Phase 6)
    "solver/cycle_lock.py",  # nimbus #1306 (spec 007, Phase 7a)
    "solver_inputs/*.py",
    "solver_reports/*.py",
)


def writer_source_paths() -> list[pathlib.Path]:
    """`solver_writer.py` first, then every module extracted out of it."""
    paths = [_NIMBUS_DIR / "solver_writer.py"]
    for pattern in _EXTRACTED:
        for path in sorted(_NIMBUS_DIR.glob(pattern)):
            if path.name == "__init__.py":
                # Package docstring and re-exports only -- no solve logic, so
                # nothing here is a call site worth searching for.
                continue
            paths.append(path)
    return paths


def writer_source() -> str:
    """The concatenated source of every module above.

    Each file is separated by a newline so a literal can never be formed
    accidentally by one file's last line abutting the next file's first.
    """
    return "\n".join(
        p.read_text(encoding="utf-8") for p in writer_source_paths() if p.exists()
    )


def writer_trees() -> list[tuple[pathlib.Path, ast.Module]]:
    """Each module above parsed separately, in the same order.

    Separately, and not as one parse of `writer_source()`, because the
    concatenation is deliberately NOT valid Python: several of these modules
    open with `from __future__ import annotations`, which the language
    requires to be the first statement in a file. Text searches over the
    concatenation are fine; an `ast.parse` of it raises.
    """
    out = []
    for path in writer_source_paths():
        if not path.exists():
            continue
        out.append(
            (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        )
    return out


def find_function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The one top-level or nested `def <name>` across the writer's modules.

    Replaces the common `ast.walk(ast.parse(solver_writer.py))` lookup, which
    stopped finding anything #1298 has relocated even though the function is
    still there, still called, and still re-exported from the facade.
    """
    for path, tree in writer_trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ):
                return node
    searched = ", ".join(p.name for p in writer_source_paths() if p.exists())
    raise AssertionError(
        f"{name} not found in any of the writer's modules ({searched}). "
        "If it was renamed or deleted that is a real finding; if it MOVED, "
        "this helper already follows it, so check the module list in "
        "tests/_writer_source.py."
    )


def function_source(name: str) -> str:
    """The exact source text of `def <name>`, from whichever module holds it.

    `ast.get_source_segment(text, node)` needs the node and the text to come
    from the SAME parse -- pairing a node found in `solver_reports/quality.py`
    with `solver_writer.py`'s text silently returns the wrong lines rather
    than failing. So this resolves both together.
    """
    for path, tree in writer_trees():
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ):
                seg = ast.get_source_segment(text, node)
                if seg is not None:
                    return seg
    raise AssertionError(f"{name} not found in any of the writer's modules")


def find_constant(name: str):
    """The literal value of a module-level `<name> = ...` across the modules.

    Only plain literals; raises for anything `ast.literal_eval` cannot take,
    which is the same limit the hand-rolled versions of this had.
    """
    for _path, tree in writer_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets
            ):
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in any of the writer's modules")
