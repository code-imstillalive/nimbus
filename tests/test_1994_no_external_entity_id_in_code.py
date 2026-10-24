"""nimbus #1994: no external entity_id is written into the integration's
code; every external source is found through the registry
(`known_sources`) or configured. A gate that reads the package source, kept
apart until the decision on source-reading tests (#1837); the registry
lookup is in tests/v2/setup/test_known_sources.py."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"

_ENTITY_ID = re.compile(
    r"^(?:sensor|binary_sensor|switch|number|select|input_number|input_boolean|"
    r"input_select|weather|water_heater|climate|button|time|device_tracker|"
    r"person|sun|zone)\.[a-z0-9_]+$"
)


def _docstring_ids(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            out.add(id(body[0].value))
    return out


def test_no_external_entity_id_is_written_into_the_integration():
    """A string constant that is a whole entity_id, outside a docstring, must
    be one of Nimbus's own. Anything else is found through the registry
    (`known_sources`) or configured."""
    found = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and _ENTITY_ID.match(node.value)
                and ".nimbus" not in node.value
            ):
                found.append(f"{path.relative_to(PKG)}:{node.lineno} {node.value}")
    assert found == []
