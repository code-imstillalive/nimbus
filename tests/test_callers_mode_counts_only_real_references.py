"""`--callers` must not count a module's own local definition as a reference.

## The defect

`analyse_module_dependencies.py --callers` reports, per name, who resolves it
from outside the module under analysis. Its output is the input to the plan's own
**façade criterion** (`docs/architecture/tech-debt-plan.md` section 2), applied
one name at a time — so an over-count on one name is an over-count on one
architectural decision.

`_referenced_names()` collected every `ast.Name` id and every `ast.Attribute`
attr, whatever object the attribute sat on. For a name this package also defines
locally, that counts the definition as a use.

Measured on `main` while verifying #1347's façade numbers:

| name | reported | real |
|---|---:|---:|
| `_LOGGER` | **19** | **6** |

Thirteen of the nineteen were modules containing their own
`_LOGGER = logging.getLogger(__name__)`, depending on `solver_writer` for
nothing at all. The six real ones are all `solver_inputs/*`, which is exactly the
layer violation `#1338`'s contract records — a much sharper fact than "19, used
everywhere", and the opposite conclusion about *why* the name needs a façade.

The three other names Spec 001 moves were unaffected (`ha_get` 3, `ha_post_state`
2, `fetch_entity_history_range` 2, before and after). They are distinctive enough
that a bare name match is a real reference. **The bug bites exactly for generic
names**, and `_LOGGER` is the most generic name in the tree — so the one name
most likely to be misjudged was the one being misreported.

## What did NOT change, checked before shipping

Phase 2's published façade figure — *"two production callers and 11 monkeypatch
sites"* (#1316) — is identical before and after: 2. So this correction does not
invalidate any number already quoted in a spec or an issue. Worth pinning,
because a measurement tool whose output silently shifts under published figures
is worse than one that over-counts predictably.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
import unittest
from pathlib import Path

_TOOL = Path(__file__).resolve().parent / "analyse_module_dependencies.py"
_spec = importlib.util.spec_from_file_location("_amd_under_test", _TOOL)
assert _spec and _spec.loader
_amd = importlib.util.module_from_spec(_spec)
sys.modules["_amd_under_test"] = _amd
_spec.loader.exec_module(_amd)


def _names_for(source: str, tmp: Path) -> set[str]:
    f = tmp / "sample.py"
    f.write_text(textwrap.dedent(source), encoding="utf-8")
    return _amd._referenced_names(f)


class TestALocalDefinitionIsNotAReference(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)

    def test_a_module_defining_its_own_logger_is_not_a_caller(self):
        """The exact 13-of-19 case. Every HA module does this."""
        names = _names_for(
            """
            import logging
            _LOGGER = logging.getLogger(__name__)

            def go():
                _LOGGER.warning("hello")
            """,
            self.tmp,
        )
        self.assertNotIn(
            "_LOGGER",
            names,
            "a module that defines and uses its OWN _LOGGER depends on "
            "solver_writer for nothing -- counting it inflated the façade "
            "input 3x on this name",
        )

    def test_reaching_into_the_module_IS_a_caller(self):
        """The six real ones. `sw.X` is how this package reaches solver_writer."""
        names = _names_for(
            """
            from .. import solver_writer as sw

            def go():
                sw._LOGGER.warning("hello")
                sw.ha_get("sensor.x")
            """,
            self.tmp,
        )
        self.assertIn("_LOGGER", names)
        self.assertIn("ha_get", names)

    def test_an_explicit_import_of_the_name_IS_a_caller(self):
        names = _names_for(
            """
            from ..solver_writer import ha_post_state

            def go():
                ha_post_state("sensor.x", "1")
            """,
            self.tmp,
        )
        self.assertIn("ha_post_state", names)

    def test_an_attribute_on_an_unrelated_object_is_not_a_caller(self):
        """`other._LOGGER` says nothing about solver_writer."""
        names = _names_for(
            """
            import some_other_module as other

            def go():
                other._LOGGER.warning("hello")
            """,
            self.tmp,
        )
        self.assertNotIn("_LOGGER", names)

    def test_a_docstring_mention_is_still_not_a_caller(self):
        """The property the tool already had, kept. Docstrings in this codebase
        cross-reference functions constantly."""
        names = _names_for(
            '''
            """See solver_writer.ha_get() for the real thing."""

            def go():
                return 1
            ''',
            self.tmp,
        )
        self.assertNotIn("ha_get", names)


class TestTheFixIsNotVacuous(unittest.TestCase):
    def test_the_aliases_the_package_actually_uses_are_recognised(self):
        """If `_MODULE_ALIASES` ever stopped containing `sw`, every real caller
        would drop to zero and the tool would under-report — which is worse than
        the over-count it replaced, because it would silently retire façades
        that are load-bearing."""
        self.assertIn("sw", _amd._MODULE_ALIASES)
        self.assertIn("solver_writer", _amd._MODULE_ALIASES)

    def test_the_real_tree_now_finds_zero_logger_callers(self):
        """nimbus issue #1301 (spec 001, #1347), Phase 2a: this used to pin
        exactly six real `sw._LOGGER` references (the six `solver_inputs/*`
        modules #1338 flagged as layer violations for exactly this name).
        Phase 2a retargets all six to `solver_shared._LOGGER` directly
        (`_LOGGER` now lives in `solver_shared.py`, not `solver_writer.py`),
        so the real, correct count is now 0 -- this is Phase 2a's own
        success condition for `_LOGGER` specifically, not the tool
        regressing. See `test_the_real_tree_still_finds_the_native_hass_
        callers` below for the alias-set non-vacuity check this test used
        to also cover."""
        package = (
            Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
        )
        hits = [
            p.relative_to(package).as_posix()
            for p in _amd._py_files(package, skip={"solver_writer.py"})
            if "_LOGGER" in _amd._referenced_names(p)
        ]
        self.assertEqual(
            hits,
            [],
            f"expected zero real sw._LOGGER references now that Phase 2a "
            f"retargets every solver_inputs/*.py module onto "
            f"solver_shared._LOGGER directly, got {hits}",
        )

    def test_the_real_tree_still_finds_the_native_hass_callers(self):
        """The non-vacuity check `_LOGGER` used to provide (`if this drops
        to 0 the alias set is wrong`) needs a name that's still a real
        `sw.<name>` reference after Phase 2a. `_NATIVE_HASS` is one of the
        names pyproject.toml's own `nimbus-layers` contract comment names
        as an unrelated, still-live reason the six `solver_inputs/*.py`
        modules keep their `solver_writer` exception -- `solver_shared.py`
        itself also reaches for it, via the same deferred `_solver_writer()`
        seam, which Phase 2a's own Migration explicitly authorises."""
        package = (
            Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
        )
        hits = [
            p.relative_to(package).as_posix()
            for p in _amd._py_files(package, skip={"solver_writer.py"})
            if "_NATIVE_HASS" in _amd._referenced_names(p)
        ]
        self.assertEqual(
            len(hits),
            5,
            f"expected exactly five real sw._NATIVE_HASS references "
            f"(battery_participants.py, extra_batteries.py, "
            f"controllable_load_history.py, controllable_loads.py, and "
            f"solver_shared.py itself), "
            f"got {hits}. This is an explicit inventory, deliberately: it "
            f"rises when a module legitimately starts reaching for the "
            f"native seam -- controllable_load_history.py (nimbus issue "
            f"#768) is the fourth -- and it must never drop to 0, which is "
            f"the non-vacuity property this test exists to hold.",
        )


class TestPublishedFiguresAreUnchanged(unittest.TestCase):
    """A measurement tool that silently shifts numbers already quoted in specs
    is worse than one that over-counts predictably."""

    def test_phase_2_still_reports_two_production_callers(self):
        package = (
            Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
        )
        # PHASES is keyed by STRING phase number and maps straight to a tuple
        # of function names -- checked against the module rather than assumed,
        # after an earlier version of this test guessed a dict.
        targets = _amd.PHASES["2"]
        # The tool's own aggregate is `sum(len(v) for v in production.values())`
        # -- (name, file) PAIRS, not distinct files. Reproduced here rather than
        # reinvented: an earlier version of this test counted distinct files,
        # got 1, and "failed" for a reason that had nothing to do with the fix.
        # Both Phase 2 pairs are `services.py` resolving two different names.
        # Skip solver_reports/ as well as solver_writer.py. nimbus issue
        # #1301 Phase 2b/2c actually PERFORMED this extraction, so those
        # functions now LIVE there, and the cluster's own internal sibling
        # references (`sw._soc_discrepancy_stats(...)` inside
        # `_compute_report_for_window()`, and so on) are not external
        # callers. Counting them took this from 2 to 5 and would have read
        # as "the extraction grew the caller surface", which is the opposite
        # of what happened -- the number #1316 published is about how many
        # OTHER production files reach in, and that is still two.
        _relocated = {
            p.relative_to(package).as_posix()
            for p in (package / "solver_reports").glob("*.py")
        }
        pairs = [
            (target, p.relative_to(package).as_posix())
            for p in _amd._py_files(package, skip={"solver_writer.py"})
            if p.relative_to(package).as_posix() not in _relocated
            for target in targets
            if target in _amd._referenced_names(p)
        ]
        self.assertEqual(
            len(pairs),
            2,
            f"#1316 published 'two production callers' for Phase 2; this "
            f"correction must not move it: {sorted(pairs)}",
        )
        self.assertEqual(
            {f for _t, f in pairs},
            {"services.py"},
            "both pairs should be services.py -- if a second file appears, the "
            "figure means something different from what #1316 published",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
