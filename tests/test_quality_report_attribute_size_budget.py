"""Recorder-cap guard for the quality report, measured against the
payload `publish_daily_quality_report()` actually publishes.

`test_solver_push_attribute_size_budget.py` does this for the two
flagship Solver push sensors. The quality report had no equivalent, and
it is the sensor with the thinnest margin in the project: its own class
comment records a payload measured at 14,609 bytes against the
Recorder's 16,384-byte cap, which is why five attributes are excluded
from recording at all.

## Why a guard here, measured rather than assumed

Two numbers off a real install (2026-09-20, reference household):

```
                            today (4 days)   at the 60-day cap
RECORDED (native path)           2,386 B          ~8,056 B   49% of cap
FULL (cron / mirror path)       19,139 B         ~24,809 B  151% of cap
```

**The recorded remainder is a growth curve, not a constant.** `history`
is trimmed to `_QUALITY_HISTORY_MAX_DAYS` (60) and that install is only
four days in, so today's comfortable 14.6% is not the number that
matters -- at the documented cap it is ~49%, and every new scalar
attribute (this guard exists because #1149 added one) moves that number
while today's reading barely twitches.

So the budget below is asserted against the **projection**, scaling the
measured per-day history cost to the retention cap. A guard that only
measured today would stay green for another eight weeks and then be
measuring something else entirely.

## What the FULL column means, and what it does not

The full payload already exceeds the cap. That is not a live defect on a
native install: HA applies `_unrecorded_attributes` through the entity
path, so only the RECORDED column is ever measured there -- confirmed on
that same install, which logs zero "exceed maximum size" warnings for
this entity.

It matters on any publish path that cannot carry `state_info` -- the
standalone/cron writer's REST POST, and a `remote_homeassistant` mirror.
That is nimbus issue #944's mechanism, and this sensor is a third
instance of it that #944 does not name. Not asserted here, because there
is nothing this repo can assert about a payload HA will measure whole:
it is over, and the fix is #944's open structural decision, not a
threshold.

## The rules

1. **Nothing large is recorded.** Every attribute NOT in the class's own
   `_unrecorded_attributes` must serialise under `_BIG_ATTR_BYTES`. This
   is the rule that catches the next per-hour dict added without being
   excluded -- the exact shape of #625.
2. **The projected recorded remainder stays under budget**, with the
   history table at its documented retention cap.

Both carry a sanity assertion that they are not passing vacuously --
the failure mode of a structural test is measuring nothing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_writer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor

# homeassistant.components.recorder.db_schema.MAX_STATE_ATTRS_BYTES,
# hardcoded for the same reason the sibling budget file hardcodes it:
# the stub harness has no recorder package, and this is a stable part of
# HA's storage contract rather than something this repo tracks.
_RECORDER_CAP_BYTES = 16384

# An order of magnitude above the largest recorded attribute measured on
# a real install (`history` at 405 B for four days) and well below the
# smallest excluded one (`hourly_regret` at 293 B is the smallest of the
# five, but the other four are 3.1-4.5 KB). Deliberately not round.
_BIG_ATTR_BYTES = 2048

# Three quarters of the cap, on the sibling file's reasoning: fail while
# there is still room to think, not at the cliff.
#
# Two projections, both real and worth keeping apart: ~8,056 B on the
# live install measured above, and ~9,882 B on this file's own fixture
# (which carries a slightly fatter history row, 122 B/day against 101).
# The fixture is what this test actually asserts on, so 12,288 leaves it
# ~2,400 B of headroom -- roughly six more attributes the size of the one
# whose addition prompted this guard. Close enough to be a real tripwire,
# which is the finding rather than a reason to loosen it.
_PROJECTED_BUDGET_BYTES = 12288

BRISBANE = solver_writer.LOCAL_TZ
NOW = datetime(2026, 8, 25, 6, 30, tzinfo=BRISBANE)
DAY_START = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)


def _size(value) -> int:
    """Serialised size, the way the Recorder measures a state's
    attributes (`JSON_DUMP` over the dict)."""
    return len(json.dumps(value, default=str))


def _cfg():
    return {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_soc_sensor": "sensor.combined_soc",
        "solver_battery_capacity_kwh": 50.0,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 10.0,
        "solver_max_discharge_kw": 10.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.1,
        "solver_grid_max_import_kw": 20.0,
        "solver_grid_max_export_kw": 20.0,
    }


def _flat(value, start, end, step_minutes=15):
    out, t = [], start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _prices(start, end, cheap=0.05, expensive=0.35, expensive_hour=17):
    out, t = [], start
    while t < end:
        out.append((t, expensive if t.hour >= expensive_hour else cheap))
        t += timedelta(minutes=15)
    return out


def _fetch(entity_id, start, end):
    if entity_id == "sensor.real_solar":
        return _flat(0.0, start, end)
    if entity_id == "sensor.real_load":
        return _flat(2.0, start, end)
    if entity_id == "sensor.real_battery":
        return _flat(3.0, start, end)
    if entity_id == "sensor.import_price":
        return _prices(start, end)
    if entity_id == "sensor.export_price":
        return _prices(start, end, cheap=0.02, expensive=0.10)
    if entity_id == "sensor.combined_soc":
        return _flat(50.0, start, end)
    return []


@pytest.fixture(scope="module")
def published():
    """Drive the REAL publish path once and return (state, attrs).

    Module-scoped: this reaches a genuine oracle solve and every test
    below reads the same payload. `ha_get` returns a sensor that has
    never been scored, so the idempotency fast path cannot short-circuit
    the compute -- a fixture that silently re-published a cached state
    would make every measurement below meaningless.
    """
    posted: dict = {}

    def _capture(entity_id, state, attrs=None):
        posted[entity_id] = (state, attrs)

    with (
        patch.object(
            solver_writer,
            "ha_get",
            return_value={"state": "unknown", "attributes": {}},
        ),
        patch.object(solver_writer, "fetch_entity_history_range", side_effect=_fetch),
        patch.object(solver_writer, "ha_post_state", side_effect=_capture),
    ):
        solver_writer.publish_daily_quality_report(_cfg(), NOW)

    assert solver_writer.QUALITY_ENTITY_ID in posted, (
        "publish_daily_quality_report() never published -- this fixture must "
        "reach a real scored day for any measurement below to mean anything"
    )
    return posted[solver_writer.QUALITY_ENTITY_ID]


@pytest.fixture(scope="module")
def excluded() -> frozenset:
    return sensor.NimbusSolverQualityReportSensor._unrecorded_attributes


class TestTheMeasurementIsBoundToTheRightClass:
    """Without this the file measures one payload and asserts about an
    unrelated frozenset."""

    def test_the_class_owns_the_entity_id_that_is_published_to(self):
        cls = sensor.NimbusSolverQualityReportSensor
        assert f"sensor.{cls._UNIQUE_ID_SUFFIX}" == solver_writer.QUALITY_ENTITY_ID

    def test_every_excluded_key_actually_appears_in_the_payload(
        self, published, excluded
    ):
        # An exclusion for a key that is not published excludes nothing,
        # and would make rule 1 below pass by measuring an empty set.
        _, attrs = published
        missing = sorted(k for k in excluded if k not in attrs)
        assert not missing, (
            f"_unrecorded_attributes names {missing}, which this payload does "
            "not contain -- either the attribute was renamed or the exclusion "
            "is dead, and either way something large may now be recorded"
        )


class TestNothingLargeIsRecorded:
    """Rule 1 -- the #625 shape: an attribute is added to the payload and
    not to the exclusion set, the row goes over the cap, and EVERY
    attribute on it loses its history silently."""

    def test_every_recorded_attribute_is_under_the_threshold(self, published, excluded):
        _, attrs = published
        oversize = {
            k: _size(v)
            for k, v in attrs.items()
            if k not in excluded and _size(v) > _BIG_ATTR_BYTES
        }
        assert not oversize, (
            f"recorded attributes over {_BIG_ATTR_BYTES} B: {oversize}. Either "
            "exclude them via NimbusSolverQualityReportSensor."
            "_unrecorded_attributes or keep them small -- an oversize row "
            "drops EVERY attribute on this sensor, not just the offender."
        )

    def test_the_rule_is_not_passing_vacuously(self, published, excluded):
        # At least one attribute must genuinely be over the threshold and
        # excluded, or this rule is measuring nothing.
        _, attrs = published
        big_and_excluded = [
            k for k in excluded if k in attrs and _size(attrs[k]) > _BIG_ATTR_BYTES
        ]
        assert big_and_excluded, (
            "no excluded attribute is actually large on this fixture, so the "
            "threshold rule above proves nothing -- check the fixture still "
            "reaches a real scored day with real hourly series"
        )


class TestTheProjectedRemainderStaysUnderBudget:
    """Rule 2 -- and the reason this file exists rather than a one-line
    assertion on today's size.

    `history` grows to `_QUALITY_HISTORY_MAX_DAYS` and is by far the
    fastest-growing recorded attribute. Measuring only what is published
    today understates the real recorded payload by roughly a factor of
    three on a young install.
    """

    def test_the_remainder_projected_to_full_history_is_under_budget(
        self, published, excluded
    ):
        _, attrs = published
        recorded = {k: v for k, v in attrs.items() if k not in excluded}
        total = _size(recorded)

        history = attrs.get("history") or {}
        assert history, (
            "this fixture published no history rows, so the projection below "
            "would silently be a no-op"
        )
        per_day = _size(history) / len(history)
        projected = (
            total - _size(history) + per_day * solver_writer._QUALITY_HISTORY_MAX_DAYS
        )

        assert projected <= _PROJECTED_BUDGET_BYTES, (
            f"recorded remainder projects to {projected:,.0f} B once history "
            f"reaches its {solver_writer._QUALITY_HISTORY_MAX_DAYS}-day cap "
            f"(today: {total:,} B with {len(history)} day(s)), over the "
            f"{_PROJECTED_BUDGET_BYTES:,} B budget and heading for the "
            f"{_RECORDER_CAP_BYTES:,} B Recorder cap. Either exclude the new "
            "attribute or shrink it."
        )

    def test_the_budget_is_a_real_fraction_of_the_cap(self):
        # A budget at or above the cap would make the test above
        # unfalsifiable in the only direction that matters.
        assert _PROJECTED_BUDGET_BYTES < _RECORDER_CAP_BYTES


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
