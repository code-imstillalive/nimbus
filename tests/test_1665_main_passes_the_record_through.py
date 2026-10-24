"""nimbus #1665: main() passes the flex telemetry record through the
load-fallback helper. A check that reads source text, kept apart until the
decision on source-reading tests (#1837); the helper is in
tests/v2/orchestration/test_load_fallback_record.py."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer

ERROR = "load forecast sensor sensor.x has no forecast attribute"


def test_main_passes_the_record_through_the_helper():
    """The record's call site, not a copy of the rule, decides this."""
    tree = ast.parse(Path(solver_writer.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "publish_flex_telemetry_record"
    ]
    assert calls, "publish_flex_telemetry_record is no longer called"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        value = kw.get("house_load_kw")
        assert isinstance(value, ast.Call), ast.dump(value) if value else None
        assert getattr(value.func, "id", None) == "_measured_house_load_kw"
