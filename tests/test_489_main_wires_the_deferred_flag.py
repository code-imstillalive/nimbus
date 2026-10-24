"""nimbus #489: `main()` hands `flex_ranging_deferred` to both flex
publishers, or the hold never engages in production.

The cadence and the hold themselves are tested in
`tests/v2/orchestration/test_plan_assembly.py` (#1837).
"""

from __future__ import annotations

import ast
import inspect

import _solver_path  # noqa: F401
import solver_writer


def test_main_passes_the_deferred_flag_to_both_publishers():
    tree = ast.parse(inspect.getsource(solver_writer.main))
    wired = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            for kw in node.keywords:
                if (
                    kw.arg == "hold_last"
                    and isinstance(kw.value, ast.Attribute)
                    and kw.value.attr == "flex_ranging_deferred"
                ):
                    wired.add(name)
    assert wired == {"publish_flex_signals", "publish_flex_telemetry_record"}
