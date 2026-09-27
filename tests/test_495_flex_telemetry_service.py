"""nimbus issue #495: `nimbus_load.flex_telemetry_record`, the pull side.

A READ of the published entity, never a rebuild. A rebuilding service
would be a second code path that can disagree with the sensor about the
same interval -- the drift shape #116 and #1141 both were -- and it would
hand out a record built for whatever instant the call landed on rather
than the boundary-aligned one the feed is keyed on.

Split from `tests/test_495_flex_telemetry_record.py` rather than appended
to it because this file imports `services.py` (and therefore the whole
integration package) while that one deliberately imports only
`flex_telemetry` and `solver_writer`, to keep the record contract
testable without standing anything up.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

from homeassistant.core import SupportsResponse

from custom_components.nimbus_load import flex_telemetry, services

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parent.parent / "schema" / "telemetry.schema.json"
    ).read_text(encoding="utf-8")
)

_ENTITY_ID = "sensor.nimbus_flex_telemetry"


def _record() -> dict:
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
        assets=[],
    )
    assert build.record is not None, build.reason
    return build.record


def _call(state):
    hass = MagicMock()
    hass.states.get.side_effect = {_ENTITY_ID: state}.get
    return asyncio.run(
        services._async_handle_flex_telemetry_record(hass, SimpleNamespace(data={}))
    )


def test_it_returns_the_record_from_the_entity_unchanged():
    record = _record()
    result = _call(
        SimpleNamespace(
            state=record["interval_start_utc"],
            attributes={
                "record": record,
                "clamped_fields": ["price_signal_seen"],
                "generated_at": "2026-09-27T14:31:02+10:00",
            },
        )
    )
    assert result["record"] == record
    jsonschema.validate(instance=result["record"], schema=_SCHEMA)
    assert result["interval_start_utc"] == record["interval_start_utc"]
    assert result["clamped_fields"] == ["price_signal_seen"]
    assert result["generated_at"] == "2026-09-27T14:31:02+10:00"


def test_a_missing_entity_returns_a_reason_not_a_raise():
    # Flex signals off is the DEFAULT state of this integration, not a
    # caller error -- a ServiceValidationError would make an ordinary
    # configuration read look like a fault.
    result = _call(None)
    assert result["record"] is None
    assert "flex_signals_enabled" in result["reason"]
    # The real measured cost belongs where a household reads it.
    assert "9x" in result["reason"]


def test_an_entity_with_no_record_attribute_returns_a_reason():
    result = _call(SimpleNamespace(state="unknown", attributes={"friendly_name": "x"}))
    assert result["record"] is None
    assert result["reason"]


def test_the_service_is_registered_with_a_response():
    hass = MagicMock()
    hass.services.has_service.return_value = False
    services.async_register_services(hass)
    calls = [
        c
        for c in hass.services.async_register.call_args_list
        if c.args[1] == services.SERVICE_FLEX_TELEMETRY_RECORD
    ]
    assert len(calls) == 1
    # Not a bare `True`: HA's own checks are identity comparisons against
    # SupportsResponse members, so `True` only works by accident.
    assert calls[0].kwargs["supports_response"] is SupportsResponse.OPTIONAL


def test_the_service_is_unregistered_on_unload():
    # #365's own point: a removed hub must not leave a callable service
    # bound to a torn-down entry.
    hass = MagicMock()
    hass.services.has_service.return_value = True
    services.async_unregister_services(hass)
    removed = {c.args[1] for c in hass.services.async_remove.call_args_list}
    assert services.SERVICE_FLEX_TELEMETRY_RECORD in removed


def test_the_service_has_a_services_yaml_and_strings_entry():
    # A service HA cannot label is a service nobody finds in the UI, and
    # hassfest checks the pairing -- asserted here too so a local run
    # catches it before CI does.
    repo = Path(__file__).resolve().parent.parent
    yaml_text = (
        repo / "custom_components" / "nimbus_load" / "services.yaml"
    ).read_text(encoding="utf-8")
    assert "\nflex_telemetry_record:" in yaml_text
    for name in ("strings.json", "translations/en.json"):
        data = json.loads(
            (repo / "custom_components" / "nimbus_load" / name).read_text(
                encoding="utf-8"
            )
        )
        entry = data["services"]["flex_telemetry_record"]
        assert entry["name"]
        assert entry["description"]
