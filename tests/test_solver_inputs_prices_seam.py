"""nimbus issue #735 stage 4: `build_price_arrays()` in
`solver_inputs/prices.py`.

**These are seam tests, not behavioural tests, and the distinction is
stated rather than blurred.** Stage 5's own test file could drive
`resolve_soc_envelope()` directly because it is pure arithmetic. This
block is not: it reaches twenty `solver_writer` helpers, several of which
perform real recorder and AEMO fetches, so driving both arms end to end
would mean standing up twenty mocks whose shapes are themselves the thing
most likely to drift. The behaviour is unchanged by the move -- the body
is byte-identical to what stood in `main()` -- so what needs guarding is
the SEAM, and that is what this file does.

**Why the seam is worth guarding at all.** The extraction turned an
`if`/`else` whose two arms merely had to agree with each other into a
function with a single unconditional `return`. That converts a silent
inconsistency into a crash:

    before   an output assigned in only one arm meant a later NameError,
             on installs of one shape only
    after    the same mistake raises immediately, on every install

The invariant making that safe is **every returned name is assigned in
BOTH arms**, which was verified once before moving. `test_both_arms_
assign_every_returned_field` is what keeps it true, and it is the one
test here that would catch a real future bug: adding a tenth output to
the rich-sensor arm and forgetting the generic arm would pass every other
check in this suite and fail only on an install without a forecast-array
price sensor.

**Why `has_price_forecast_array` is a parameter.** `main()` reads it again
after this call, to gate the percentile-band reuse. Computing it in both
places would make one question have two sources of truth, which is the
drift this project keeps finding (see #582's duplicated period-index
resolution, which then did not inherit its own fix). Pinned below so it
stays a parameter.
"""

from __future__ import annotations

import ast
import os
import unittest

import _solver_path  # noqa: F401
from solver_inputs import prices

EXPECTED_FIELDS = (
    "spot_import_raw",
    "spot_export",
    "import_real_mask",
    "export_real_mask",
    "import_price_upper_band",
    "export_price_lower_band",
    "export_bonus_price",
    "match_fraction",
    "p2p_recent_volume_kwh",
)


def _module_source(mod):
    with open(mod.__file__.replace(".pyc", ".py"), encoding="utf-8") as f:
        return f.read()


def _build_price_arrays_ast():
    tree = ast.parse(_module_source(prices))
    return next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "build_price_arrays"
    )


def _solver_writer_source():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "custom_components", "nimbus_load", "solver_writer.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestTheResultShape(unittest.TestCase):
    def test_price_arrays_carries_exactly_the_expected_fields(self):
        """Adding a field without wiring it into the return, or wiring a
        return value without declaring it, both fail here."""
        self.assertEqual(
            tuple(prices.PriceArrays.__dataclass_fields__), EXPECTED_FIELDS
        )

    def test_it_is_frozen(self):
        """Same reasoning as LoadArrays: the record is stable even though
        the arrays inside it are not."""
        self.assertTrue(prices.PriceArrays.__dataclass_params__.frozen)

    def test_the_signature_is_the_measured_seam(self):
        """5 inputs, matching the figure Mark Purcell independently
        re-measured on #735. If this grows, the seam has moved and the
        issue's ledger needs updating rather than quietly diverging."""
        fn = _build_price_arrays_ast()
        args = [a.arg for a in fn.args.args]
        self.assertEqual(
            args,
            [
                "cfg",
                "grid_times",
                "n_periods",
                "now",
                "has_price_forecast_array",
                "price_forecast_sensor",
            ],
        )


class TestTheInvariantThatMakesTheReturnSafe(unittest.TestCase):
    """The one test here that can catch a real future bug."""

    def test_both_arms_assign_every_returned_field(self):
        fn = _build_price_arrays_ast()
        branch = next(s for s in fn.body if isinstance(s, ast.If))

        def stores(nodes):
            found = set()
            for n in nodes:
                for x in ast.walk(n):
                    if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                        found.add(x.id)
            return found

        if_arm, else_arm = stores(branch.body), stores(branch.orelse)
        for field in EXPECTED_FIELDS:
            with self.subTest(field=field):
                self.assertIn(
                    field,
                    if_arm,
                    f"{field} is returned but never assigned in the "
                    "has_price_forecast_array arm -- this raises NameError "
                    "on any install WITH a forecast-array price sensor",
                )
                self.assertIn(
                    field,
                    else_arm,
                    f"{field} is returned but never assigned in the generic "
                    "arm -- this raises NameError on any install WITHOUT a "
                    "forecast-array price sensor, which is the majority",
                )

    def test_the_branch_is_still_one_statement(self):
        """The property that made the move a pure dedent. If someone
        splits this into two top-level `if`s, the #873 re-parenting trap
        is back in play and the arms can silently disagree again."""
        fn = _build_price_arrays_ast()
        ifs = [s for s in fn.body if isinstance(s, ast.If)]
        self.assertEqual(len(ifs), 1)
        self.assertTrue(ifs[0].orelse, "the else arm has gone")


class TestTheCallSite(unittest.TestCase):
    def test_main_rebinds_every_returned_field(self):
        """A field added to PriceArrays but never unpacked in main() is
        dead weight that reads as wired."""
        src = " ".join(_solver_writer_source().split())
        for field in EXPECTED_FIELDS:
            with self.subTest(field=field):
                self.assertIn(f"{field} = _prices.{field}", src)

    def test_main_still_computes_the_flag_itself(self):
        """It is a parameter precisely because main() needs it after the
        call. If it moves inside, the second reader below silently loses
        its gate."""
        src = " ".join(_solver_writer_source().split())
        self.assertIn("has_price_forecast_array = bool(price_forecast_sensor)", src)

    def test_the_flag_is_still_read_after_the_call(self):
        """The reason the parameter exists, asserted rather than only
        explained: main() gates the percentile-band reuse on it."""
        lines = _solver_writer_source().split("\n")
        call = next(
            i for i, l in enumerate(lines) if "price_inputs.build_price_arrays(" in l
        )
        later = "\n".join(lines[call:])
        self.assertIn(
            "if has_price_forecast_array:",
            later,
            "nothing reads the flag after the call any more, so it no "
            "longer needs to be a parameter -- fold it into "
            "build_price_arrays() and drop it from the signature",
        )


class TestSharedHelpersStayBoundThroughSolverWriter(unittest.TestCase):
    """Same property stage 5's own test file pins, for the same reason: an
    install with `custom_components.nimbus_load.solver_writer: debug` set
    must still control these lines after the move, and a monkeypatch of a
    `solver_writer` helper must still be honoured here.

    That only holds while this module reaches them as ATTRIBUTES of the
    module object rather than importing them by name -- see
    `solver_inputs/__init__.py` and #861.
    """

    def test_no_helper_is_imported_by_name_from_solver_writer(self):
        tree = ast.parse(_module_source(prices))
        offenders = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and "solver_writer" in node.module
            ):
                offenders.extend(a.name for a in node.names)
        self.assertEqual(
            offenders,
            [],
            "imported by name from solver_writer, which binds at import "
            f"time and defeats every monkeypatch and log-level override: "
            f"{offenders}",
        )

    def test_the_accessor_is_the_deferred_by_module_one(self):
        self.assertTrue(hasattr(prices, "_solver_writer"))
        sw = prices._solver_writer()
        self.assertTrue(hasattr(sw, "_LOGGER"))
        self.assertTrue(hasattr(sw, "fetch_price_history"))

    def test_aemo_crosscheck_is_reached_through_solver_writer(self):
        """It is imported in `solver_writer` and noqa'd there because this
        module is now its only consumer. If this module ever imports it
        directly, that noqa becomes a lie and the single-binding-point
        arrangement is broken."""
        src = _module_source(prices)
        self.assertIn("sw.aemo_crosscheck", src)
        self.assertNotIn("import aemo_crosscheck", src)


if __name__ == "__main__":
    unittest.main()
