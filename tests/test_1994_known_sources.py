"""nimbus #1994: every external source is found one way, through the entity
registry by integration and built-in unique_id, never by entity_id.

Solar auto-include, the Solcast running-low gate, the #1932 new-install
default and LocalVolts' price freshness used to match fixed entity_ids, so a
renamed or localised entity was silently lost: with no other solar source,
every plan ran on 0 kW solar. Each now reads what `known_sources.resolve()`
finds, published on sensor.nimbus_solver_config as `known_sources`.
"""

from __future__ import annotations

import ast
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import solver_writer
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from solver_inputs import price_freshness, solar

from custom_components.nimbus_load import known_sources as ks

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"

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


def test_a_renamed_solcast_is_still_found_today_first():
    found = _resolve(
        _solcast("sensor.bumbum_solbum_today", "sensor.bumbum_solbum_tmrw")
    )
    assert found["solcast"] == [
        "sensor.bumbum_solbum_today",
        "sensor.bumbum_solbum_tmrw",
    ]


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


# --- the readers use it -------------------------------------------------------


def test_a_renamed_localvolts_price_keeps_its_freshness_check():
    lv = {
        "entities": ["sensor.lv_buy_now", "sensor.lv_price", "sensor.lv_sell_now"],
        "stamp_siblings": ["sensor.lv_buy_now", "sensor.lv_sell_now"],
    }
    assert (
        price_freshness.lv_v2_siblings(["sensor.lv_price"], lv) == lv["stamp_siblings"]
    )
    assert price_freshness.lv_v2_siblings(["sensor.amber_price"], lv) == []
    assert price_freshness.lv_v2_siblings(["sensor.lv_price"], None) == []


def test_a_renamed_solcast_still_joins_the_solar_blend():
    """Before #1994 auto-include looked for Solcast's default entity_id and,
    renamed, read nothing; now the renamed entity is the member."""
    now = datetime(2026, 10, 22, 10, 0).astimezone()
    grid = [now + timedelta(minutes=30 * i) for i in range(4)]
    detailed = [
        {
            "period_start": (now + timedelta(minutes=30 * i)).isoformat(),
            "pv_estimate": 3.0,
            "pv_estimate10": 2.0,
            "pv_estimate90": 4.0,
        }
        for i in range(4)
    ]
    states = {
        "sensor.bumbum_solbum_today": {"attributes": {"detailedForecast": detailed}}
    }
    cfg = {"known_sources": {"solcast": ["sensor.bumbum_solbum_today"]}}
    with (
        patch.object(solver_writer, "entity_exists", lambda e: e in states),
        patch.object(solver_writer, "ha_get", lambda e: states[e]),
    ):
        value, lower, upper = solar._fetch_solcast_solar_raw(grid, len(grid), cfg)
        assert solar._fetch_solcast_solar_raw(grid, len(grid), {}) is None
    assert value == [3.0] * 4 and lower == [2.0] * 4 and upper == [4.0] * 4


# --- no entity_id literal comes back -------------------------------------------

_ENTITY_ID = re.compile(
    r"^(?:sensor|binary_sensor|switch|number|select|input_number|input_boolean|"
    r"input_select|weather|water_heater|climate|button|time|device_tracker|"
    r"person|sun|zone)\.[a-z0-9_]+$"
)


def _docstring_ids(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            out.add(id(body[0].value))
    return out


def test_no_external_entity_id_is_written_into_the_integration():
    """A string constant that is a whole entity_id, outside a docstring, must
    be one of Nimbus's own. Anything else is found through the registry
    (`known_sources`) or configured."""
    found = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and _ENTITY_ID.match(node.value)
                and ".nimbus" not in node.value
            ):
                found.append(f"{path.relative_to(PKG)}:{node.lineno} {node.value}")
    assert found == []
