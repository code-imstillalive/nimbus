"""nimbus issues #1067 / #448: the Sources step's optional fields collapse
behind a single "Advanced" group, so a new installer reads 2 entries instead
of 16 to get a working Solver.

## Why this step

#1067 names it: the form with the most fields, and "the highest chance of
being pointed somewhere wrong (see #111 and #118 for what 'pointed somewhere
wrong' costs)". @purcell-lab's standing principle, from #449 and restated on
#485, is fewer wizard values surfaced, not more.

## Nothing is removed and nothing is renamed

Every field keeps its key, its marker class, its default/`suggested_value` and
its validator — it renders inside a collapsed group. The wizard's own field
count is **unchanged at 47**, which `test_config_surface_budget` independently
pins. This is about what a new installer must READ before they can submit, not
about taking capability away from an existing one.

## The hazard this file mostly exists for

A `section` **nests** its fields one level deep in the submitted `user_input`.
`_absorb_step()` does a flat `.update()` and then NULLS every key of its own
schema that is absent from the submission — which is how a genuinely-cleared
optional field stays cleared (#341, Mark Purcell).

Without flattening, every field inside the section is absent from the top
level, so **one submit would null all fourteen of them** — silently wiping real
configuration. That is the wizard-wipe class this repo has already had three
of.

`TestSubmittingThroughASectionDoesNotWipeAnything` is the test that matters
here. It drives the real `_absorb_step` with a realistically-nested submission
and asserts the values survive; it fails loudly on an unflattened
implementation.

## And the mirror-image hazard

Flattening alone is not enough. The null sweep iterates the schema's own keys,
so against a wrapped schema it would see the section's key (`"advanced"`) and
never the fields inside it — meaning a genuinely cleared field would silently
keep its old value, reintroducing #341 from the other direction, and
`"advanced": None` would be written into stored options as a stray key.
`_effective_schema_keys()` exists for that, and is tested for both halves.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import voluptuous as vol
from homeassistant.data_entry_flow import section

from custom_components.nimbus_load.flows import hub_options as ho

_ADV = ho._ADVANCED_SECTION


def _keys(schema: vol.Schema) -> list[str]:
    return [str(getattr(m, "schema", m)) for m in schema.schema]


def _toy() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("req_a"): str,
            vol.Optional("opt_a"): str,
            vol.Optional("opt_b"): str,
        }
    )


class TestTheCollapse(unittest.TestCase):
    def test_required_fields_stay_at_the_top_level(self):
        out = ho._collapse_optionals_into_advanced(_toy())
        self.assertIn("req_a", _keys(out))

    def test_optional_fields_move_inside_the_section(self):
        out = ho._collapse_optionals_into_advanced(_toy())
        self.assertNotIn("opt_a", _keys(out))
        self.assertIn(_ADV, _keys(out))
        inner = next(
            v for m, v in out.schema.items() if str(getattr(m, "schema", m)) == _ADV
        )
        self.assertEqual(sorted(_keys(inner.schema)), ["opt_a", "opt_b"])

    def test_the_section_is_collapsed(self):
        """Not merely grouped -- collapsed. An expanded group would still
        show all sixteen fields, which is the thing being fixed."""
        out = ho._collapse_optionals_into_advanced(_toy())
        inner = next(
            v for m, v in out.schema.items() if str(getattr(m, "schema", m)) == _ADV
        )
        self.assertTrue(inner.options.get("collapsed"))

    def test_keep_visible_promotes_a_field_back(self):
        out = ho._collapse_optionals_into_advanced(_toy(), keep_visible=("opt_a",))
        self.assertIn("opt_a", _keys(out))

    def test_a_schema_with_no_optionals_is_returned_untouched(self):
        """No empty section on a step that has nothing to collapse."""
        plain = vol.Schema({vol.Required("only"): str})
        self.assertIs(ho._collapse_optionals_into_advanced(plain), plain)

    def test_no_field_is_lost_or_renamed(self):
        """The whole safety argument for regrouping an already-built schema
        instead of rewriting the declarative literal by hand."""
        before = _solver_keys(ho._solver_sources_schema({}))
        after = _solver_keys(
            ho._collapse_optionals_into_advanced(ho._solver_sources_schema({}))
        )
        self.assertEqual(before, after)


def _solver_keys(schema: vol.Schema) -> set[str]:
    """Every real field key, sections expanded."""
    out: set[str] = set()
    for marker in schema.schema:
        validator = schema.schema[marker]
        if isinstance(validator, section):
            out |= {str(getattr(m, "schema", m)) for m in validator.schema.schema}
        else:
            out.add(str(getattr(marker, "schema", marker)))
    return out


class TestSubmittingThroughASectionDoesNotWipeAnything(unittest.TestCase):
    """The wizard-wipe guard. This repo has had three bugs of this shape."""

    def _flow(self) -> Any:
        flow = ho.NimbusHubOptionsFlow.__new__(ho.NimbusHubOptionsFlow)
        flow._solver_data = {}
        flow._ensure_solver_data_seeded = lambda: None
        return flow

    def test_nested_values_survive_the_absorb(self):
        schema = ho._collapse_optionals_into_advanced(_toy())
        flow = self._flow()
        flow._absorb_step(
            schema, {"req_a": "kept", _ADV: {"opt_a": "also kept", "opt_b": "kept too"}}
        )
        self.assertEqual(flow._solver_data["req_a"], "kept")
        self.assertEqual(flow._solver_data["opt_a"], "also kept")
        self.assertEqual(flow._solver_data["opt_b"], "kept too")

    def test_the_section_key_is_never_stored(self):
        """`"advanced"` is a rendering container, not configuration. Writing
        it into options would leave a stray key in every install's stored
        config forever."""
        schema = ho._collapse_optionals_into_advanced(_toy())
        flow = self._flow()
        flow._absorb_step(schema, {"req_a": "x", _ADV: {"opt_a": "y"}})
        self.assertNotIn(_ADV, flow._solver_data)

    def test_an_omitted_nested_field_is_STILL_cleared(self):
        """The mirror-image hazard. #341: a field the household actually
        cleared must not silently keep its old value. The null sweep has to
        see through the section, or collapsing would reintroduce that bug."""
        schema = ho._collapse_optionals_into_advanced(_toy())
        flow = self._flow()
        flow._solver_data = {"opt_a": "stale old value"}
        flow._absorb_step(schema, {"req_a": "x", _ADV: {"opt_b": "set"}})
        self.assertIsNone(
            flow._solver_data["opt_a"],
            "an optional field omitted from a submitted section was cleared "
            "by the household and must not keep its previous value (#341)",
        )

    def test_a_flat_submission_still_works(self):
        """Not every step is wrapped. The flattening must be a no-op for a
        schema that has no section at all."""
        flow = self._flow()
        flow._absorb_step(_toy(), {"req_a": "x", "opt_a": "y"})
        self.assertEqual(flow._solver_data["opt_a"], "y")


class TestTheSourcesStepUsesIt(unittest.TestCase):
    def test_only_the_two_required_sources_are_visible(self):
        collapsed = ho._collapse_optionals_into_advanced(ho._solver_sources_schema({}))
        visible = [k for k in _keys(collapsed) if k != _ADV]
        self.assertEqual(len(visible), 2, f"expected 2 visible, got {visible}")

    def test_the_form_and_the_absorb_use_the_SAME_wrapping(self):
        """If the form collapsed but the absorb did not (or vice versa), the
        null sweep would run against a different shape than the submission --
        the exact mismatch that produces a silent wipe."""
        import inspect

        show = inspect.getsource(ho.NimbusHubOptionsFlow.async_step_solver_sources)
        self.assertEqual(
            show.count("_collapse_optionals_into_advanced"),
            2,
            "both the async_show_form schema and the _absorb_step schema must "
            "be wrapped identically",
        )


class TestTheWizardDidNotGrow(unittest.TestCase):
    def test_the_hub_wizard_key_count_is_unchanged(self):
        """#448 is about this number. Collapsing must not change it -- the
        fields still exist, they are just not all on screen at once."""
        total = (
            len(ho._SOLVER_WIZARD_SCHEMA_KEYS)
            + len(ho._FORECASTER_SCHEMA_KEYS)
            + len(ho._SWITCHBOARD_SCHEMA_KEYS)
        )
        self.assertEqual(total, 47)


class TestTheTranslationsExist(unittest.TestCase):
    """A section with no translation renders as its raw key, and hassfest
    fails on it."""

    def test_both_files_carry_the_section(self):
        import json

        base = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"
        for name in ("strings.json", "translations/en.json"):
            blob = json.loads((base / name).read_text(encoding="utf-8"))
            sec = blob["options"]["step"]["solver_sources"]["sections"][_ADV]
            with self.subTest(file=name):
                self.assertTrue(sec["name"])
                self.assertIn("blank", sec["description"].lower())


class TestTheGridStepAlsoCollapses(unittest.TestCase):
    def test_only_the_two_price_sensors_are_visible(self):
        collapsed = ho._collapse_optionals_into_advanced(ho._solver_grid_schema({}))
        visible = [k for k in _keys(collapsed) if k != _ADV]
        self.assertEqual(len(visible), 2, f"expected 2 visible, got {visible}")

    def test_the_form_and_the_absorb_use_the_SAME_wrapping(self):
        import inspect

        src = inspect.getsource(ho.NimbusHubOptionsFlow.async_step_solver_grid)
        self.assertEqual(src.count("_collapse_optionals_into_advanced"), 2)

    def test_no_field_is_lost(self):
        before = _solver_keys(ho._solver_grid_schema({}))
        after = _solver_keys(
            ho._collapse_optionals_into_advanced(ho._solver_grid_schema({}))
        )
        self.assertEqual(before, after)


class TestWhichStepsAreDeliberatelyNotCollapsed(unittest.TestCase):
    """Measured, not assumed. Collapsing is only an improvement when
    something stays visible; a form that is nothing but a collapsed box is
    worse than the flat list it replaced.

        step             required  optional
        solver_sources          2        14   collapsed
        solver_grid             2         7   collapsed
        solver_battery          1         1   not worth it
        forecaster              0        12   would leave an EMPTY form
        switchboard             0         8   would leave an EMPTY form

    Forecaster and Switchboard have no required fields at all. Making them
    useful would mean promoting some optional fields via `keep_visible` --
    i.e. deciding which of twelve are the "real" decisions for every
    household, which is @purcell-lab's territory (#449 / #485) and wants
    evidence rather than my guess.
    """

    def test_the_all_optional_steps_are_not_wrapped(self):
        import inspect

        for step in ("async_step_forecaster", "async_step_switchboard"):
            src = inspect.getsource(getattr(ho.NimbusHubOptionsFlow, step))
            with self.subTest(step=step):
                self.assertNotIn("_collapse_optionals_into_advanced", src)

    def test_those_steps_really_do_have_no_required_fields(self):
        """Pins the measurement the decision rests on. If one ever gains a
        required field, collapsing it becomes worth reconsidering."""
        for builder in (ho._forecaster_schema, ho._switchboard_schema):
            required = [m for m in builder({}).schema if isinstance(m, vol.Required)]
            with self.subTest(builder=builder.__name__):
                self.assertEqual(required, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
