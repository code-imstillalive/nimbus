"""nimbus #1681: no bare import of a sibling module outside an ImportError fallback.

`solver_inputs/controllable_load_history.py` imported `from solver.elements
import ...` at function scope with no relative-import attempt first. Under
pytest that resolves, because `pyproject.toml` puts
`custom_components/nimbus_load` on `sys.path`. In a real HA install it does
not, so `build_oracle_controllable_loads()` raised `ModuleNotFoundError` on
every scored day and #1357's oracle wiring never ran anywhere (Mark Purcell,
found live on his install, 10 Oct 2026).

The package's own convention is: relative import first, bare absolute import
only inside the `except ImportError:` fallback, for the standalone/cron
deployment that has no parent package. This test enforces it statically,
for the same reason `test_relative_import_depth_resolves.py` is static:
importing the modules here would succeed through the very sys.path entry
that masked the bug.

Rule: inside `custom_components/nimbus_load/`, an absolute import whose top
name is a module or package that lives in that directory must sit inside an
`except` handler. The `solver/` and `ml/` packages themselves are exempt
(they are the pure standalone libraries and import each other by design).
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
EXEMPT_DIRS = {"solver", "ml"}


def _sibling_names() -> set[str]:
    names = set()
    for p in PKG.iterdir():
        if p.is_dir() and (p / "__init__.py").exists():
            names.add(p.name)
        elif p.suffix == ".py" and p.stem != "__init__":
            names.add(p.stem)
    return names


def _violations() -> list[str]:
    siblings = _sibling_names()
    found = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG)
        if rel.parts[0] in EXEMPT_DIRS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                for inner in ast.walk(node):
                    guarded.add(id(inner))
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top in siblings:
                    found.append(f"{rel}:{node.lineno} from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in siblings:
                        found.append(f"{rel}:{node.lineno} import {alias.name}")
    return found


def test_no_unguarded_bare_import_of_a_sibling_module():
    violations = _violations()
    assert not violations, (
        "Bare absolute imports of a package sibling outside an `except "
        "ImportError:` fallback resolve only under pytest's sys.path, not in "
        "real HA (#1681). Import relatively first:\n  " + "\n  ".join(violations)
    )


def test_the_1681_site_is_now_guarded():
    """Pins the exact regression: the oracle's imports are relative first."""
    src = (PKG / "solver_inputs" / "controllable_load_history.py").read_text(
        encoding="utf-8"
    )
    assert "from ..solver.elements import" in src
    assert "from ..solver.quality_report import" in src
