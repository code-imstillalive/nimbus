"""Generic recorder-cap guard for the two flagship Solver push sensors,
measured against the payload `main()` actually publishes.

nimbus issue **#625** is the bug class this closes here. Its own shape:
`plan_cost_forecast` and `temperature_forecast` were added to a published
payload and never added to `_unrecorded_attributes`, so the Recorder hit
its 16 KB per-state attribute cap and dropped **the whole row's**
attributes -- not just the oversized series. Everything else on that
sensor lost its history too, silently, for as long as it took someone to
notice.

#625's own fix came in two halves, and only the second one is durable:
a fixed-set assertion catches a *regression* of an already-known field,
while a generic check catches the *next* new one. For
`NimbusControllableLoadStateSensor` that generic half exists and
introspects `LoadRunState`'s own dataclass fields
(`test_sensor_forecast_unrecorded_attributes.py`, last test). The two
flagship sensors here have the fixed-set half only
(`test_sensor_solver_push_entities.py`) -- and no dataclass to
introspect, because their attributes are a hand-built dict literal
inside `main()` and inside `solver_publish.publish_household_load_total_
forecast()`. A new key is one line in a 60-key dict.

So this file takes the source of truth from the only place it is
honestly available: **the real published payload**. It drives the real
`main()` through the same fixture the #363 golden-output guardrail
already established (`test_main_golden_output_guardrail._make_ha_get()`,
reused rather than re-fixtured so there is exactly one definition of
"a fixture that reaches a real optimal solve"), captures what
`ha_post_state()` receives for both entities, and applies two rules.

**Rule 1 -- per-period series.** Any list-valued attribute with one
entry per forecast period is a per-period series and must be excluded.
Size-independent, so it holds on a real 18-load household exactly as it
does on this small fixture.

**Rule 2 -- size.** Any attribute serialising above `_BIG_ATTR_BYTES`
must be excluded. Needed because rule 1 does not see `batteries`, whose
length is the number of participants (1 here), not the number of
periods -- while each element carries a full per-period series inside
it. 23,344 bytes on this fixture alone.

Neither rule subsumes the other, so both carry a sanity assertion that
they actually caught something -- the failure mode of a structural test
is passing vacuously.

**Thresholds, from real measured values on this fixture** (not picked
round): the largest *recorded* attribute is `cost_breakdown` at 174
bytes; the two excluded ones are 268,707 (`forecast`) and 23,344
(`batteries`). `_BIG_ATTR_BYTES = 2048` sits an order of magnitude above
the former and an order below the latter. The recorded remainder totals
2,432 bytes against the Recorder's 16,384 cap, so `_RECORDED_BUDGET_
BYTES = 8192` is a warning shot at half the cap with 3.3x headroom --
the point is to fail while there is still room to think, not at the
cliff, which is what #625 did.

Deliberately NOT asserting the exact excluded set: that is
`test_sensor_solver_push_entities.py`'s job and duplicating it here
would make this file fail for a reason it is not about.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_writer
from test_main_golden_output_guardrail import _make_ha_get

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor

# homeassistant.components.recorder.db_schema.MAX_STATE_ATTRS_BYTES.
# Hardcoded rather than imported: the stub harness does not provide the
# recorder package, and this number is a stable part of HA's storage
# contract, not something this repo tracks release to release.
_RECORDER_CAP_BYTES = 16384

_BIG_ATTR_BYTES = 2048
_RECORDED_BUDGET_BYTES = 8192

_BATTERY = "sensor.nimbus_solver_battery_forecast"
_LOAD_TOTAL = "sensor.nimbus_household_load_total_forecast"

# The binding that makes this file's measurements mean anything: the
# payload measured below is published to this entity_id, and this class
# is the one whose _unrecorded_attributes decides what the Recorder
# keeps from it. Asserted, not assumed -- see the first test.
_CLASS_FOR = {
    _BATTERY: sensor.NimbusSolverBatteryForecastSensor,
    _LOAD_TOTAL: sensor.NimbusHouseholdLoadTotalForecastSensor,
}


def _size(value) -> int:
    """Serialised size, the way the Recorder itself measures a state's
    attributes (`JSON_DUMP` over the whole dict). `default=str` only
    covers a value this fixture's own solve never produces -- a real
    non-serialisable attribute would be a different bug, caught
    elsewhere."""
    return len(json.dumps(value, default=str))


@pytest.fixture(scope="module")
def published():
    """Run the real main() once and return {entity_id: (state, attrs)}.

    Module-scoped: this is a genuine LP solve (~1.5s) and every test
    below reads the same payload. Time is frozen for the same reason the
    golden guardrail freezes it -- `build_tiered_grid()`'s period count
    depends on the real wall clock, and a size budget that moves with the
    hour is a flake waiting to happen.
    """
    from freezegun import freeze_time

    posted: dict[str, tuple] = {}

    def _capture(entity_id, state, attrs=None):
        posted[entity_id] = (state, attrs)

    with (
        freeze_time("2026-09-06T10:00:00+00:00"),
        patch.object(solver_writer, "ha_get", side_effect=_make_ha_get()),
        patch.object(solver_writer, "ha_post_state", side_effect=_capture),
        patch.object(solver_writer, "acquire_lock", return_value=True),
        patch.object(solver_writer, "release_lock"),
        patch.object(
            solver_writer,
            "PLAN_STATE_PATH",
            "/tmp/nonexistent_plan_state_attr_budget_test.json",
        ),
    ):
        solver_writer.main()

    missing = [e for e in _CLASS_FOR if e not in posted]
    assert not missing, (
        f"main() never published {missing} -- this fixture must reach a "
        "real optimal solve for any measurement below to mean anything"
    )
    return posted


class TestTheMeasurementIsBoundToTheRightClass:
    """Without this, the file measures one payload and asserts about an
    unrelated frozenset."""

    @pytest.mark.parametrize("entity_id", sorted(_CLASS_FOR))
    def test_the_class_owns_the_entity_id_main_publishes_to(self, entity_id):
        cls = _CLASS_FOR[entity_id]
        assert f"sensor.{cls._UNIQUE_ID_SUFFIX}" == entity_id

    @pytest.mark.parametrize("entity_id", sorted(_CLASS_FOR))
    def test_the_entity_is_natively_managed(self, entity_id):
        """A raw `states.async_set()` write carries no entity, therefore
        no `_unrecorded_attributes` at all -- the exclusion only exists
        on the native path. `_NATIVE_MANAGED_ENTITY_IDS` is what keeps
        these two off the fallback."""
        assert entity_id in solver_writer._NATIVE_MANAGED_ENTITY_IDS


class TestNoPerPeriodSeriesIsRecorded:
    """Rule 1. Structural and size-independent: it holds identically on a
    real household publishing 18 circuits over 96 hours."""

    @pytest.mark.parametrize("entity_id", sorted(_CLASS_FOR))
    def test_every_one_period_per_entry_list_is_excluded(self, entity_id, published):
        _state, attrs = published[entity_id]
        excluded = _CLASS_FOR[entity_id]._unrecorded_attributes
        n_periods = len(attrs["forecast"])
        assert n_periods > 1, "fixture must produce a real multi-period horizon"

        per_period = {
            k for k, v in attrs.items() if isinstance(v, list) and len(v) == n_periods
        }
        assert per_period, (
            "sanity check: this rule found no per-period series at all on "
            f"{entity_id}, so it would pass for any payload whatsoever"
        )
        leaked = per_period - excluded
        assert not leaked, (
            f"{sorted(leaked)} are per-period series published on {entity_id} "
            "but NOT in its _unrecorded_attributes -- the Recorder will drop "
            "this sensor's ENTIRE attribute row once the 16 KB cap is hit, "
            "taking every other attribute's history with it (nimbus issue "
            f"#625). Add them to {_CLASS_FOR[entity_id].__name__}'s base "
            "class frozenset in sensor.py."
        )


class TestNoLargeAttributeIsRecorded:
    """Rule 2. Catches what rule 1 structurally cannot -- `batteries` is
    one entry per participant, not per period, and is 23 KB on a fixture
    with a single battery."""

    @pytest.mark.parametrize("entity_id", sorted(_CLASS_FOR))
    def test_every_attribute_over_the_threshold_is_excluded(self, entity_id, published):
        _state, attrs = published[entity_id]
        excluded = _CLASS_FOR[entity_id]._unrecorded_attributes

        big = {k for k, v in attrs.items() if _size(v) > _BIG_ATTR_BYTES}
        assert big, (
            "sanity check: nothing on "
            f"{entity_id} exceeded {_BIG_ATTR_BYTES} bytes, so this rule "
            "passed without examining anything"
        )
        leaked = big - excluded
        assert not leaked, (
            f"{sorted(leaked)} each serialise above {_BIG_ATTR_BYTES} bytes "
            f"on {entity_id} and are NOT in its _unrecorded_attributes. The "
            "Recorder's cap is per-STATE, not per-attribute, so a few of "
            "these together drop the whole row (nimbus issue #625)."
        )

    def test_the_two_rules_are_not_the_same_rule(self, published):
        """Stated as its own case because it is the reason both exist: on
        the battery sensor, `batteries` is caught by size and invisible to
        the per-period rule. Delete either rule and this asymmetry is what
        goes unguarded."""
        _state, attrs = published[_BATTERY]
        n_periods = len(attrs["forecast"])
        batteries = attrs["batteries"]
        assert len(batteries) != n_periods
        assert _size(batteries) > _BIG_ATTR_BYTES


class TestTheRecordedRemainderFitsWithRoom:
    """What actually reaches the database. #625's real damage was not the
    oversized series -- it was everything else losing its history."""

    @pytest.mark.parametrize("entity_id", sorted(_CLASS_FOR))
    def test_recorded_attributes_stay_well_under_the_cap(self, entity_id, published):
        _state, attrs = published[entity_id]
        excluded = _CLASS_FOR[entity_id]._unrecorded_attributes
        recorded = {k: v for k, v in attrs.items() if k not in excluded}
        total = _size(recorded)
        assert total < _RECORDED_BUDGET_BYTES, (
            f"{entity_id}'s recorded attributes now total {total} bytes "
            f"against a {_RECORDED_BUDGET_BYTES}-byte budget (the Recorder's "
            f"own hard cap is {_RECORDER_CAP_BYTES}). This is deliberately a "
            "warning shot with headroom left, not the cliff -- either "
            "exclude the newest large attribute, or raise the budget "
            "consciously and say why."
        )

    def test_the_budget_is_a_real_fraction_of_the_cap(self):
        """Guards the guard: a budget quietly raised to or past the cap
        would keep passing while guarding nothing."""
        assert _RECORDED_BUDGET_BYTES <= _RECORDER_CAP_BYTES // 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
