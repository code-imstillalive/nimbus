"""Every external source Nimbus reads is found one way: through the entity
registry, by its integration and built-in unique_id, never by entity_id, so a
renamed or localised entity is still found (#1994) (#1837).

An ambiguous, disabled or unloaded source is not read. Published on
sensor.nimbus_solver_config as `known_sources`.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

ks = importlib.import_module("custom_components.nimbus_load.known_sources")

# The entity_ids these integrations create on an install that renamed nothing:
# what the fixed lists used to hold.
DEFAULT_SOLCAST = [
    "sensor.solcast_pv_forecast_forecast_today",
    "sensor.solcast_pv_forecast_forecast_tomorrow",
]
DEFAULT_OPEN_METEO = [
    "sensor.home_energy_production_today",
    "sensor.home_energy_production_tomorrow",
    *(f"sensor.home_energy_production_d{d}" for d in range(2, 8)),
]


def _reg(uid, entity_id, disabled=None, domain="sensor"):
    return SimpleNamespace(
        unique_id=uid, entity_id=entity_id, disabled_by=disabled, domain=domain
    )


def _site(entries: dict[str, list[tuple[str, list]]], state="loaded"):
    """`entries`: domain -> [(entry_id, [registry rows])]."""
    by_entry = {}
    hass = MagicMock()
    made = {}
    for domain, rows in entries.items():
        made[domain] = []
        for entry_id, regs in rows:
            made[domain].append(
                SimpleNamespace(state=SimpleNamespace(value=state), entry_id=entry_id)
            )
            by_entry[entry_id] = regs
    hass.config_entries.async_entries = MagicMock(side_effect=lambda d: made.get(d, []))
    return hass, by_entry


def _resolve(entries, state="loaded"):
    hass, by_entry = _site(entries, state)
    with (
        patch.object(ks.er, "async_get", return_value=MagicMock()),
        patch.object(
            ks.er,
            "async_entries_for_config_entry",
            side_effect=lambda _reg_, entry_id: by_entry.get(entry_id, []),
        ),
    ):
        return ks.resolve(hass)


def _solcast(today, tomorrow):
    return {
        ks.SOLCAST_DOMAIN: [
            (
                "S1",
                [
                    _reg("total_kwh_forecast_tomorrow", tomorrow),
                    _reg("total_kwh_forecast_today", today),
                    _reg("api_counter", "sensor.solcast_api_used"),
                ],
            )
        ]
    }


def _open_meteo(entry_id, names):
    return [
        (
            entry_id,
            [
                _reg(f"{entry_id}_{k}", n)
                for k, n in zip(ks.OPEN_METEO_KEYS, names, strict=True)
            ]
            + [_reg(f"{entry_id}_power_production_now", "sensor.power_now")],
        )
    ]


# --- the registry lookup ------------------------------------------------------


def test_an_install_that_renamed_nothing_gets_exactly_the_old_entities():
    """The identity on the default path: the registry's answer is the very
    list the fixed entity_ids held, so nothing changes for such an install."""
    found = _resolve(
        {
            **_solcast(*DEFAULT_SOLCAST),
            ks.OPEN_METEO_DOMAIN: _open_meteo("OM1", DEFAULT_OPEN_METEO),
        }
    )
    assert found["solcast"] == DEFAULT_SOLCAST
    assert found["open_meteo"] == DEFAULT_OPEN_METEO


def test_a_renamed_open_meteo_is_still_found():
    names = [f"sensor.dach_{i}" for i in range(8)]
    found = _resolve({ks.OPEN_METEO_DOMAIN: _open_meteo("OM1", names)})
    assert found["open_meteo"] == names


def test_two_open_meteo_entries_are_ambiguous_and_neither_is_read():
    found = _resolve(
        {
            ks.OPEN_METEO_DOMAIN: _open_meteo("OM1", DEFAULT_OPEN_METEO)
            + _open_meteo("OM2", [f"sensor.other_{i}" for i in range(8)])
        }
    )
    assert found["open_meteo"] == []


def test_a_disabled_or_unloaded_source_is_not_read():
    regs = _solcast(*DEFAULT_SOLCAST)
    regs[ks.SOLCAST_DOMAIN][0][1][1].disabled_by = "user"  # today's
    assert _resolve(regs)["solcast"] == DEFAULT_SOLCAST[1:]
    assert _resolve(_solcast(*DEFAULT_SOLCAST), state="setup_retry")["solcast"] == []


def test_nothing_installed_is_the_empty_shape():
    assert _resolve({}) == ks.EMPTY


def test_localvolts_is_found_by_its_unique_ids():
    found = _resolve(
        {
            ks.LV_V2_DOMAIN: [
                (
                    "L1",
                    [
                        _reg("L1_current_buy_rate", "sensor.lv_buy_now"),
                        _reg("L1_current_sell_rate", "sensor.lv_sell_now"),
                        _reg("L1_buy_flex_up", "sensor.lv_price"),
                    ],
                )
            ]
        }
    )
    assert found["localvolts_v2"] == {
        "entities": ["sensor.lv_buy_now", "sensor.lv_price", "sensor.lv_sell_now"],
        "stamp_siblings": ["sensor.lv_buy_now", "sensor.lv_sell_now"],
    }
