"""nimbus issue #467 item 4: the collection surface for a calendar-driven
departure, and the precedence rule that makes it safe to add.

## Why this is being added at all, having argued against it

I checked the reference household and found `scored_participants: ['home']`, no
EV entity, and **no `calendar.*` entity at all** — and concluded the feature had
no purpose, so the config surface should not be built. The household's answer:

> we have created fake test entities on devhub... didnt we? so we could test? we
> do not build this for me only.... it is for global scene and participants...

They were right. `mqtt_pseudo_device_fleet.py` exists precisely so devhub can
exercise *"the whole Controllable Load AND multi-battery-participant config
surface without any of it touching a real household appliance"*, and
`binary_sensor.nimbus_test_ev_nimbus_test_ev_available` is live there today. I had
applied a local test — *does this house own an EV* — to a feature built for other
installs. Recorded here because the reasoning error is more reusable than the fix.

## Where it lives, and why that is not a contradiction

@purcell-lab's standing "no more fields" principle (#449, #485) is about the
three-step **hub** wizard, and his own count was of exactly those three schemas:
switchboard 8 + forecaster 12 + solver 27 = 47. These three go on the **per-EV
Battery Participant subentry** — a form that only appears when a household
deliberately adds a second battery or an EV. An install with no participants never
sees it.

`TestTheHubWizardDidNotGrow` pins that separation, so this cannot later be read
as licence to grow the wizard everyone walks.

## Three entries, one real decision

`kwh_per_100km` carries a default (18, mid-range for a passenger EV), so the
calendar entity is the only thing a household must choose. That is the "sensible
defaults" half of the same principle rather than a fourth thing to research.

The default deliberately errs mid-range rather than optimistic: under-estimating
consumption under-charges for the trip, and the failure that matters is a
stranded car, not a slightly fuller pack.

## The precedence rule

The calendar **overrides** the fixed `departure_hour`/percent pair when it
resolves a trip in this horizon, and otherwise the fixed pair stands untouched.
That ordering is the whole safety argument for adding this: an install configured
only with the fixed pair behaves exactly as it did before, and a calendar that is
empty, unavailable, beyond the horizon, or holding an event with no distance
falls back rather than clearing a deadline someone configured.

## What is deliberately configurable but unused

The **odometer**. It is collected, and the resolution layer accepts
`already_driven_km_by_start` and is tested for it — but nothing reads it yet. An
odometer reports **total lifetime distance**, not distance into the current trip,
and converting one to the other needs a reading taken at the moment of departure,
which nothing stores. Subtracting a lifetime odometer from a trip distance would
be arithmetic on two different quantities.

Not reading it means the full trip is always required, which errs towards a
fuller pack. `TestTheOdometerIsHonestlyUnused` pins that, so the gap is visible
rather than looking like an oversight.

## Approach

Source inspection, matching how `test_1109_*` and `test_1111_*` already test
`build_extra_batteries()` — it needs real `ConfigSubentries`, which are not
constructible here. The resolution layer underneath has 28 behavioural tests of
its own, and the chain has 12 end-to-end through a real solve.
"""

from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import (
    CONF_BATTERY_PARTICIPANT_KWH_PER_100KM,
    CONF_BATTERY_PARTICIPANT_ODOMETER_ENTITY,
    CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY,
    DEFAULT_PARTICIPANT_KWH_PER_100KM,
)
from custom_components.nimbus_load.flows import battery_participant_subentry as bps
from custom_components.nimbus_load.solver_inputs import (
    extra_batteries as extra_batteries_inputs,
)

_NIMBUS = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"
_FIELDS = (
    CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY,
    CONF_BATTERY_PARTICIPANT_KWH_PER_100KM,
    CONF_BATTERY_PARTICIPANT_ODOMETER_ENTITY,
)


def _builder_src() -> str:
    # nimbus issue #1300: moved to solver_inputs/extra_batteries.py.
    return inspect.getsource(extra_batteries_inputs.build_extra_batteries)


class TestTheFieldsAreCollectable(unittest.TestCase):
    def test_all_three_are_in_the_participant_schema(self):
        keys = {
            (k.schema if hasattr(k, "schema") else k) for k in bps._schema({}).schema
        }
        for field in _FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, keys)

    def test_all_three_are_optional(self):
        """A participant that is a second house battery rather than a car must
        not be forced to name a calendar."""
        import voluptuous as vol

        schema = bps._schema({})
        required = {
            m.schema
            for m in schema.schema
            if isinstance(m, vol.Required) and hasattr(m, "schema")
        }
        for field in _FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field, required)

    def test_consumption_carries_the_default_so_only_the_calendar_is_a_decision(self):
        schema = bps._schema({})
        marker = next(
            m
            for m in schema.schema
            if getattr(m, "schema", None) == CONF_BATTERY_PARTICIPANT_KWH_PER_100KM
        )
        self.assertEqual(marker.default(), DEFAULT_PARTICIPANT_KWH_PER_100KM)

    def test_a_saved_consumption_survives_a_reconfigure(self):
        """The default must not overwrite a household's own figure when the form
        is reopened -- which is the wizard-wipe class this repo has had three
        of."""
        schema = bps._schema({CONF_BATTERY_PARTICIPANT_KWH_PER_100KM: 24.5})
        marker = next(
            m
            for m in schema.schema
            if getattr(m, "schema", None) == CONF_BATTERY_PARTICIPANT_KWH_PER_100KM
        )
        self.assertEqual(marker.default(), 24.5)

    def test_the_calendar_field_selects_a_calendar_not_a_sensor(self):
        """An entity selector on the wrong domain is how a household ends up
        pointing this at a sensor and seeing nothing happen, with no error.

        Searches for the constant's NAME, not its value: the source refers to
        `CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY`, and the first version of
        this test looked for the string it resolves to, which naturally is not
        there.
        """
        src = inspect.getsource(bps._schema)
        i = src.index("CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY")
        self.assertIn('domain="calendar"', src[i : i + 400])


class TestTheTranslationsExist(unittest.TestCase):
    """A config-flow field with no translation renders as its raw key. hassfest
    also fails on it, so this is a fast local signal for a slow CI one."""

    def test_every_field_has_a_label_and_a_description_in_both_files(self):
        for name in ("strings.json", "translations/en.json"):
            blob = json.loads((_NIMBUS / name).read_text(encoding="utf-8"))
            flat = json.dumps(blob)
            for field in _FIELDS:
                with self.subTest(file=name, field=field):
                    self.assertIn(field, flat)

    def _description(self, field: str) -> str:
        """The field's own data_description text, looked up through the parsed
        JSON rather than by indexing the raw file -- the first version of this
        test indexed the raw blob, landed on the LABEL occurrence, and read the
        following 1200 characters of somebody else's descriptions."""
        blob = json.loads((_NIMBUS / "strings.json").read_text(encoding="utf-8"))
        found: list[str] = []

        def walk(node):
            if not isinstance(node, dict):
                return
            desc = node.get("data_description")
            if isinstance(desc, dict) and field in desc:
                found.append(desc[field])
            for value in node.values():
                walk(value)

        walk(blob)
        self.assertTrue(found, f"no data_description for {field}")
        return found[0]

    def test_the_calendar_description_tells_a_household_what_to_type(self):
        """The distance-in-the-title convention is invisible otherwise, and an
        event with no distance is silently ignored -- so the form has to say
        so."""
        text = self._description(CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY)
        self.assertIn("km", text)
        self.assertIn("ignored", text)

    def test_the_odometer_description_says_it_is_optional_and_why(self):
        text = self._description(CONF_BATTERY_PARTICIPANT_ODOMETER_ENTITY)
        self.assertIn("Optional", text)
        self.assertIn("full trip", text)


class TestThePrecedenceRule(unittest.TestCase):
    """The safety argument for adding the fields at all."""

    def test_the_calendar_is_resolved_in_the_live_solve_builder(self):
        src = _builder_src()
        self.assertIn("resolve_trip_deadline", src)
        self.assertIn("fetch_calendar_trips", src)

    def test_it_runs_AFTER_the_fixed_pair_so_it_can_override(self):
        """Order is the mechanism. Resolving the calendar first would let the
        fixed pair overwrite the better information."""
        src = _builder_src()
        self.assertLess(
            src.index("CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR"),
            src.index("resolve_trip_deadline"),
        )

    def test_a_calendar_that_resolves_nothing_leaves_the_fixed_pair_alone(self):
        """`resolve_trip_deadline()` returns None for an empty calendar, a trip
        beyond the horizon, or an event with no distance. The assignment must be
        guarded on that, or configuring a calendar would CLEAR a deadline the
        household set by hand."""
        src = _builder_src()
        i = src.index("resolved = resolve_trip_deadline")
        window = src[i : i + 600]
        self.assertIn("if resolved is not None:", window)

    def test_the_horizon_end_uses_the_last_period_s_real_duration(self):
        """This solver's grid is tiered -- the first period is minutes wide and
        the last is an hour. A flat assumption would mis-bound the calendar
        query on exactly the horizon edge where a trip is most likely to sit."""
        src = _builder_src()
        i = src.index("horizon_end = ")
        self.assertIn("periods.hours[-1]", src[i : i + 300])


class TestTheOdometerIsHonestlyUnused(unittest.TestCase):
    """Collected, documented, and deliberately not read yet."""

    def test_the_builder_does_not_read_it(self):
        self.assertNotIn(CONF_BATTERY_PARTICIPANT_ODOMETER_ENTITY, _builder_src())

    def test_the_builder_says_why(self):
        """A collected-but-unused field looks like an oversight unless the code
        states the reason -- an odometer reports lifetime distance, not distance
        into this trip."""
        src = _builder_src()
        self.assertIn("odometer", src.lower())
        self.assertIn("lifetime", src.lower())

    def test_the_resolution_layer_still_supports_it(self):
        """So the gap is a stored departure reading, not the mechanism."""
        from custom_components.nimbus_load.solver_inputs import calendar_trips

        params = inspect.signature(calendar_trips.resolve_trip_deadline).parameters
        self.assertIn("already_driven_km_by_start", params)


class TestTheHubWizardDidNotGrow(unittest.TestCase):
    """The separation this whole placement argument rests on. If these fields
    ever migrate into the hub wizard, that is a decision to argue for -- not
    something to arrive by drift."""

    def test_none_of_the_three_is_in_a_hub_wizard_schema(self):
        from custom_components.nimbus_load.flows import hub_options

        for tup in (
            hub_options._SOLVER_WIZARD_SCHEMA_KEYS,
            hub_options._FORECASTER_SCHEMA_KEYS,
            hub_options._SWITCHBOARD_SCHEMA_KEYS,
        ):
            for field in _FIELDS:
                with self.subTest(field=field):
                    self.assertNotIn(field, tup)

    def test_the_hub_wizard_count_is_unchanged(self):
        from custom_components.nimbus_load.flows import hub_options

        total = (
            len(hub_options._SOLVER_WIZARD_SCHEMA_KEYS)
            + len(hub_options._FORECASTER_SCHEMA_KEYS)
            + len(hub_options._SWITCHBOARD_SCHEMA_KEYS)
        )
        self.assertEqual(
            total,
            47,
            "the hub wizard grew; #448 is about exactly this number and it was "
            "47 before this change -- if it moved for another reason, update "
            "this and say why",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
