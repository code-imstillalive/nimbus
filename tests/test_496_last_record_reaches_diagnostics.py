"""nimbus issue #496 (Signals 7/7 of #489), third diagnostics criterion:
*"Diagnostics contain the last emitted record and it validates against
schema v2.0."*

That criterion was blocked on #495's emitter, which did not exist -- the
vendored `schema/telemetry.schema.json` had exactly one Python reference
in the whole repo, its own drift test. With the emitter landed, the half
this file pins is the one that was never checkable before: the record
reaches the dump **byte-for-byte**, and what reaches the dump validates.

Why "byte-for-byte" is the load-bearing word. The schema declares
`additionalProperties: false`, so any reshaping on the way into a
diagnostics dump -- a spread, a filter, a rename, a key added for
readability -- can invalidate a record that was perfectly valid on the
entity. The criterion is about the dump, so the dump has to hold the same
closed object the schema test validates, not a rendering of it. That is
the same class of defect as #116 (a curated allowlist in this very file
that stopped tracking real output) approached from the other side: #116
was about dropping fields, this is about adding them.

`tests/test_496_flex_and_offer_curve_diagnostics.py` covers the other two
parts of the criterion (the flex family's payload and the offer curve's
sweep timings) and its own docstring records that this part was out of
scope at the time. This file is that note being closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import diagnostics, flex_telemetry

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parent.parent / "schema" / "telemetry.schema.json"
    ).read_text(encoding="utf-8")
)

_TELEMETRY_ENTITY_ID = "sensor.nimbus_flex_telemetry"


def _hass(states: dict[str, SimpleNamespace | None]) -> MagicMock:
    hass = MagicMock()
    hass.states.get.side_effect = states.get
    return hass


def _record() -> dict:
    """A real record from the shipped builder, not a hand-typed dict --
    so this test cannot pass against a record shape the emitter would
    never produce."""
    from datetime import UTC, datetime

    clamped: list[str] = []
    asset = flex_telemetry.build_asset(
        name="home",
        capacity_kwh=122.2,
        soc_kwh=61.1,
        charge_kw=4.0,
        discharge_kw=0.0,
        max_discharge_kw=40.0,
        available_up_kw=36.0,
        available_down_kw=40.0,
        shadow_power_balance_price=0.1644,
        clamped=clamped,
    )
    build = flex_telemetry.build_record(
        interval_start=datetime(2026, 9, 27, 4, 30, tzinfo=UTC),
        region="QLD1",
        postcode_prefix="456",
        net_import_kw=0.42,
        solar_kw=4.8,
        house_load_kw=1.2,
        deferrable_load_kw=0.0,
        naive_baseline_kw=-3.6,
        price_signal_seen=0.25,
        price_export_seen=0.09,
        envelope_import_limit_kw=42.0,
        envelope_export_limit_kw=40.0,
        flex_available_up_kw=2.1,
        flex_available_down_kw=4.3,
        shadow_energy_price=0.1644,
        shadow_solar_forecast_price=0.0,
        shadow_envelope_import_price=0.5393,
        shadow_envelope_export_price=-0.0087,
        assets=[asset],
        clamped_in=clamped,
    )
    assert build.record is not None, build.reason
    return build.record


def _published_state(record: dict) -> SimpleNamespace:
    """The attribute shape `publish_flex_telemetry_record()` posts."""
    return SimpleNamespace(
        state=record["interval_start_utc"],
        attributes={
            "friendly_name": "Nimbus Flex Telemetry",
            "record": record,
            "clamped_fields": [],
            "generated_at": "2026-09-27T14:31:02+10:00",
            "nimbus_version": "0.94.999",
        },
    )


def test_the_last_record_reaches_the_dump_and_validates():
    record = _record()
    result = diagnostics._flex_telemetry_diagnostics(
        _hass({_TELEMETRY_ENTITY_ID: _published_state(record)})
    )
    assert result["enabled"] is True
    assert result["interval_start_utc"] == record["interval_start_utc"]
    jsonschema.validate(instance=result["record"], schema=_SCHEMA)


def test_the_record_in_the_dump_is_the_record_on_the_entity_unchanged():
    record = _record()
    result = diagnostics._flex_telemetry_diagnostics(
        _hass({_TELEMETRY_ENTITY_ID: _published_state(record)})
    )
    # Not just "equal": the same keys, no additions. `additionalProperties:
    # false` means one readability key added here would break the very
    # criterion this block exists to satisfy.
    assert result["record"] == record
    assert set(result["record"]) == set(_SCHEMA["required"])


def test_the_dump_carries_the_context_a_support_request_needs():
    record = _record()
    result = diagnostics._flex_telemetry_diagnostics(
        _hass({_TELEMETRY_ENTITY_ID: _published_state(record)})
    )
    # Which install produced it (#972's own mirror check) and whether any
    # price was clamped on the way out -- both useless in a dump if only
    # the record travels.
    assert result["nimbus_version"] == "0.94.999"
    assert result["clamped_fields"] == []
    assert result["generated_at"] == "2026-09-27T14:31:02+10:00"


def test_a_missing_entity_is_enabled_false_not_a_crash():
    # The switch is off by default, so this is the NORMAL state on a fresh
    # install -- same posture as _offer_curve_diagnostics().
    assert diagnostics._flex_telemetry_diagnostics(_hass({})) == {"enabled": False}


def test_an_entity_with_no_record_attribute_reports_a_null_record():
    # Reachable for one push after a restart, or if a future publisher
    # changes shape. A `record` of the wrong type must read as absent
    # rather than reaching a schema validator as garbage.
    state = SimpleNamespace(state="unknown", attributes={"friendly_name": "x"})
    result = diagnostics._flex_telemetry_diagnostics(
        _hass({_TELEMETRY_ENTITY_ID: state})
    )
    assert result["enabled"] is True
    assert result["record"] is None


def test_the_full_dump_includes_the_flex_telemetry_block():
    # Guards the wiring, not the helper: a helper nothing calls is the
    # #538/#692 shape this repo has been burned by twice.
    import inspect

    source = inspect.getsource(diagnostics.async_get_config_entry_diagnostics)
    assert '"flex_telemetry": _flex_telemetry_diagnostics(hass)' in source
