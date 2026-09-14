"""nimbus issue #773: `solve_diagnostics.n_controllable_loads` silently
omitted thermal loads.

The field summed `sheddable_loads + adequacy_loads` only. `thermal_loads`
has been a first-class controllable-load kind since #774/#800 -- and is
the kind the one real thermal load in existence actually uses -- so any
install whose controllable loads are thermal reported zero.

Found live on devhub rather than by reading: six controllable loads were
demonstrably in the plan, their own status sensors reading
"scheduled 09:00-15:30", "running" and "done", while this field reported
`0`.

Why that was worth fixing rather than shrugging at: #773's own triage
reasoned *directly* from this number --

    "Worth checking whether the calibrated/secondary-cost lex phase has
     an edge case that a large controllable-load + multi-battery-
     participant combination can push into infeasibility"

-- so a field that under-reports load count was actively misleading an
open investigation about problem shape and solve cost.

Two layers, because neither alone is enough. The first pins the
arithmetic contract (the bug was a missing term in a sum). The second
walks `solver_writer.py`'s own AST and asserts the real publish path
actually references all three plan lists -- so the contract tests can't
quietly pass while the shipped expression drifts back.
"""

from __future__ import annotations

import unittest

import _solver_path


def _counts(n_sheddable: int, n_adequacy: int, n_thermal: int) -> dict:
    """Reproduce the diagnostics dict the publish path builds, from the
    same expression, with plan lists of the given lengths.

    Deliberately asserts on the ARITHMETIC contract rather than importing
    the whole native publish path (which needs a live hass): the bug was
    a missing term in a sum, and that is exactly what this pins.
    """
    sheddable = [object()] * n_sheddable
    adequacy = [object()] * n_adequacy
    thermal = [object()] * n_thermal
    return {
        "n_controllable_loads": len(sheddable) + len(adequacy) + len(thermal),
        "n_sheddable_loads": len(sheddable),
        "n_adequacy_loads": len(adequacy),
        "n_thermal_loads": len(thermal),
    }


class TestThermalLoadsAreCounted(unittest.TestCase):
    def test_a_thermal_only_install_no_longer_reports_zero(self):
        """The exact devhub shape that exposed this: controllable loads
        present and scheduled, total reported as 0."""
        d = _counts(0, 0, 6)
        self.assertEqual(d["n_controllable_loads"], 6)
        self.assertEqual(d["n_thermal_loads"], 6)

    def test_the_total_is_the_sum_of_all_three_kinds(self):
        d = _counts(2, 3, 4)
        self.assertEqual(d["n_controllable_loads"], 9)
        self.assertEqual(
            d["n_controllable_loads"],
            d["n_sheddable_loads"] + d["n_adequacy_loads"] + d["n_thermal_loads"],
            "the total must stay the honest sum of the per-kind counts",
        )

    def test_no_controllable_loads_still_reports_zero_everywhere(self):
        """The common case (and the golden-output fixture's own shape) --
        this change must not invent loads where there are none."""
        d = _counts(0, 0, 0)
        for key, value in d.items():
            with self.subTest(key=key):
                self.assertEqual(value, 0)

    def test_sheddable_and_adequacy_are_still_counted(self):
        """Guard against 'fixing' this by swapping one omission for
        another."""
        self.assertEqual(_counts(5, 0, 0)["n_controllable_loads"], 5)
        self.assertEqual(_counts(0, 5, 0)["n_controllable_loads"], 5)


class TestPublishPathUsesAllThreeLists(unittest.TestCase):
    """The arithmetic tests above pin the contract; this pins that the
    real publish path actually implements it, so the two can't drift."""

    def test_publish_plan_counts_thermal_loads(self):
        import ast
        import pathlib

        src = pathlib.Path(
            _solver_path._SOLVER_PARENT  # type: ignore[attr-defined]
        ).joinpath("solver_writer.py")
        tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))

        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "n_controllable_loads" not in keys:
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "n_controllable_loads"
                ):
                    found.append(ast.unparse(value))

        self.assertTrue(found, "no solve_diagnostics dict found in solver_writer.py")
        for expr in found:
            with self.subTest(expr=expr):
                for attr in ("sheddable_loads", "adequacy_loads", "thermal_loads"):
                    self.assertIn(
                        attr,
                        expr,
                        f"n_controllable_loads is computed as {expr!r}, which "
                        f"omits plan.{attr} -- that omission is the #773 bug.",
                    )


if __name__ == "__main__":
    unittest.main()
