"""DC-006 slice 2: the Forecaster's observation inputs in `active`
(`device_observation.forecaster_inputs`, #1574). Synthetic F10 states only.

What the Forecaster is fed, by mode:
* `legacy`: exactly today's readings, whatever the document says.
* `active`: the contract's reading, in the sign the model was trained with,
  held while briefly missing, then the model's training mean once absent
  (Mark Purcell's two decisions, #1574, 21 Oct 2026). A feature the model
  never used stays 0.0."""

from __future__ import annotations

import copy
from datetime import timedelta

import device_observation as do

from tests.device_contract.test_dc004_views_and_preview import F10, di
from tests.device_contract.test_dc006_observation_capture import NORMAL, NOW, _st

MEANS = {"battery_kw": -1.25, "grid_kw": 0.4, "solar_kw": 2.2}


def _doc():
    return di.import_legacy(copy.deepcopy(F10)).document


def _run(mode, states, trained, hold=None, now=NOW, own_target="sensor.pool_power"):
    def read(entity_id):
        return states.get(entity_id)

    legacy = do.legacy_inputs(F10["options"], read, "load", own_target)
    return do.forecaster_inputs(
        mode,
        _doc(),
        legacy,
        read,
        now,
        hold or do.ObservationHold(),
        trained,
        MEANS,
    )


def _values(rows):
    return {name: row["value"] for name, row in rows.items()}


def test_active_changes_nothing_for_a_model_trained_in_legacy():
    # test_active_changes_nothing_for_a_model_trained_in_legacy: The equivalence gate on a healthy F10 site: the battery's canonical
    assert _values(_run("active", NORMAL, do.TRAINED_NATIVE)) == _values(
        _run("legacy", NORMAL, do.TRAINED_NATIVE)
    )

    # test_a_missing_reading_is_held_then_the_training_mean
    hold = do.ObservationHold()
    _run("active", NORMAL, do.TRAINED_NATIVE, hold=hold)
    gone = {**NORMAL, "sensor.meter_power": _st("unavailable", "W")}
    held = _run(
        "active", gone, do.TRAINED_NATIVE, hold=hold, now=NOW + timedelta(seconds=60)
    )
    assert held["grid_power"] == {
        "value": 1.5,
        "state": "held_stale",
        "entity_id": "sensor.meter_power",
    }
    late = _run(
        "active", gone, do.TRAINED_NATIVE, hold=hold, now=NOW + timedelta(hours=2)
    )
    assert late["grid_power"]["state"] == "absent"
    assert late["grid_power"]["value"] == MEANS["grid_kw"]
    # never a fabricated zero
    assert late["grid_power"]["value"] != 0.0

    # test_a_feature_the_model_never_used_stays_zero: The model's own target is excluded today (#1540): `unused`, 0.0, in
    rows = _run("active", NORMAL, do.TRAINED_NATIVE, own_target="sensor.meter_power")
    assert rows["grid_power"] == {"value": 0.0, "state": "unused", "entity_id": None}
    assert rows["solar_power"]["value"] == 3.0

    # test_sign_flip_matches_the_binding
    doc = _doc()
    assert do.sign_flip(doc, "battery_power") == -1.0
    assert do.sign_flip(doc, "grid_power") == 1.0
    assert do.sign_flip(doc, "solar_power") == 1.0
