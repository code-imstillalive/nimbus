"""nimbus #496: the flex family and the offer curve reach the diagnostics
dump.

`async_get_config_entry_diagnostics()` returned exactly four blocks --
`entry`, `subentries`, `solver`, `solver_config` -- so a household
debugging flexibility had nothing to attach to a report, and the issue's
"ranging validity/degeneracy flags, and sweep timings" criterion had
nowhere to land.

Three design decisions are pinned here because each one is a lesson this
project has already paid for:

1. **The flex report is spread whole, never allowlisted.** nimbus #116 is
   the bug where a curated list in this very file stopped tracking
   `solver_writer.py`'s real output: `cost_breakdown` and
   `load_forecast_source_used` both shipped, both read `null` in
   diagnostics, and a reader reasonably concluded the fix had not landed.
2. **Flex SIGNAL children are resolved through the entity registry by
   unique_id, never by building `sensor.nimbus_flex_<suffix>`.** Mark
   Purcell's instruction on #768, for the controllable load's power
   sensor: a name-built id works on the install it was written on and
   breaks wherever HA has suffixed the entity_id after a collision --
   which is the standing condition on any install carrying duplicate
   Nimbus entities.
3. **The suffix list is read from `FLATTENED_ATTRS_FLEX` itself**, not
   retyped here, so a new flex signal appears in diagnostics with no
   maintenance. Same class of fix as (1), applied before it can bite.

Measured before writing any of it: `sensor.nimbus_flex_signals`' parent
carries **no payload attributes at all** on a real install -- only entity
metadata -- because every per-signal value lives on a flattened child.
Spreading that parent the way the report is spread would have captured
nothing and looked like it worked, which is why the two are handled
differently rather than uniformly.

Not covered, deliberately: this issue's other diagnostics criterion, "the
last emitted record ... validates against schema v2.0". The vendored
`schema/telemetry.schema.json` exists but its only Python reference
anywhere in the repo is its own drift test -- #495's emitter was never
built, so there is no record to dump. That half stays on #495.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import diagnostics, sensor_flattened

_ENTRY_ID = "01TESTENTRY"


def _fake_state(state, attributes: dict) -> SimpleNamespace:
    return SimpleNamespace(state=state, attributes=attributes)


def _fake_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    return entry


def _registry(mapping: dict[str, str | None]) -> MagicMock:
    """A registry that answers `async_get_entity_id` from `mapping`,
    keyed by unique_id -- so a test can prove the lookup went through
    unique_id rather than a constructed name."""
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda domain, platform, unique_id: (
        mapping.get(unique_id)
    )
    return registry


def _hass(states: dict[str, SimpleNamespace | None]) -> MagicMock:
    hass = MagicMock()
    hass.states.get.side_effect = states.get
    return hass


def _flex(hass, registry):
    with patch(
        "homeassistant.helpers.entity_registry.async_get", return_value=registry
    ):
        return diagnostics._flex_diagnostics(hass, _fake_entry())


def test_the_flex_report_is_spread_whole_not_allowlisted():
    """#116's lesson. A future key added to the report must appear here
    with no change to this file."""
    attrs = {
        "latest_date": "2026-09-17",
        "offered_up_kwh": 12.5,
        "realised_up_kwh": 9.0,
        "envelope_curtailment_kwh": 0.0,
        "price_response_curve": [[0.1, 1.0]],
        "a_field_invented_after_this_test_was_written": "present",
    }
    result = _flex(
        _hass({"sensor.nimbus_flex_report": _fake_state("9.0", attrs)}), _registry({})
    )
    assert result["report_entity_found"] is True
    assert result["report_state"] == "9.0"
    for key, value in attrs.items():
        assert result["report"][key] == value, (
            f"{key} was dropped from the diagnostics dump -- this block "
            "must spread the entity's whole attribute dict, not a curated "
            "list. That list is exactly what went stale in #116 and made "
            "two shipped fields read null here while live on the entity."
        )


def test_a_missing_flex_report_is_honest_not_a_crash():
    result = _flex(_hass({}), _registry({}))
    assert result["report_entity_found"] is False
    assert result["report"] is None
    assert result["report_state"] is None


def test_signal_children_are_resolved_by_unique_id_not_by_name():
    """#768's lesson. The registry maps this unique_id to a SUFFIXED
    entity_id -- the situation on any install with duplicate Nimbus
    entities. A name-built lookup would miss it entirely and report the
    signal as absent."""
    suffix = sensor_flattened.FLATTENED_ATTRS_FLEX[0].entity_id_suffix
    unique_id = f"{_ENTRY_ID}_nimbus_flex_{suffix}"
    real_entity_id = f"sensor.nimbus_flex_{suffix}_2"
    registry = _registry({unique_id: real_entity_id})
    hass = _hass({real_entity_id: _fake_state("3.25", {})})

    result = _flex(hass, registry)

    assert result["signals"][suffix] == {
        "entity_id": real_entity_id,
        "state": "3.25",
    }, (
        "the suffixed entity_id was not found. Building "
        f"'sensor.nimbus_flex_{suffix}' as a string would miss it, which "
        "is precisely why #768 required registry resolution"
    )


def test_a_signal_with_no_registry_entry_reads_none():
    suffix = sensor_flattened.FLATTENED_ATTRS_FLEX[0].entity_id_suffix
    result = _flex(_hass({}), _registry({}))
    assert result["signals"][suffix] is None


def test_every_flex_signal_appears_without_a_retyped_list():
    """The suffixes come from FLATTENED_ATTRS_FLEX itself, so adding a
    signal there is enough. A hand-maintained copy here is the #116 bug
    waiting to happen a second time."""
    result = _flex(_hass({}), _registry({}))
    expected = {s.entity_id_suffix for s in sensor_flattened.FLATTENED_ATTRS_FLEX}
    assert set(result["signals"]) == expected
    assert len(expected) > 1


def test_offer_curve_absent_is_disabled_not_empty():
    """The offer curve is opt-in (#494). 'Switched off' and 'switched on
    and producing nothing' are different answers and a dump must not
    conflate them."""
    result = diagnostics._offer_curve_diagnostics(_hass({}))
    assert result == {"enabled": False}


def test_offer_curve_surfaces_sweep_timings_and_the_curves():
    """#496's own criterion: sweep timings and the ranging output. Both
    are already published -- this only had to surface them."""
    attrs = {
        "sweep_seconds": 1.83,
        "import_curve": [{"price_lower": 0.1, "price_upper": 0.2, "kw": 1.2}],
        "export_curve": [],
        "price_limits": {"cap": 23.2},
    }
    result = diagnostics._offer_curve_diagnostics(
        _hass({"sensor.nimbus_offer_curve": _fake_state("1.2", attrs)})
    )
    assert result["enabled"] is True
    assert result["state"] == "1.2"
    assert result["sweep_seconds"] == 1.83
    assert result["price_limits"] == {"cap": 23.2}
    # An empty curve must be visible AS empty, not absent -- a degenerate
    # sweep is a real finding and the dump is where it gets seen.
    assert result["export_curve"] == []


def test_the_dump_gains_both_blocks():
    """The whole point: they have to be reachable from the file a
    household actually downloads."""
    import asyncio

    entry = _fake_entry()
    entry.title = "Nimbus"
    entry.options = {}
    entry.subentries = {}
    entry.runtime_data = {}
    hass = _hass({})

    with patch(
        "homeassistant.helpers.entity_registry.async_get", return_value=_registry({})
    ):
        result = asyncio.run(
            diagnostics.async_get_config_entry_diagnostics(hass, entry)
        )

    assert "flex" in result
    assert "offer_curve" in result
    assert result["offer_curve"] == {"enabled": False}
    assert result["flex"]["report_entity_found"] is False
