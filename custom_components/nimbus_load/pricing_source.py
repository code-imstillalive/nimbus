"""Switch the Solver's pricing between detected providers (nimbus #1550).

`select.nimbus_pricing_source` lists every complete pricing profile Nimbus
detects (the same detectors `pricing_autodetect.py` uses, by config entry and
built-in unique_id, never by entity name), plus **Manual**.

* **The current option is derived, not stored.** It names the profile the
  Solver's saved pricing fields match, or Manual when they match none. So it
  persists across restarts with nothing extra to keep in step, and a household
  that edits prices in Configure -> Solver: Grid Prices sees Manual.
* **Picking a provider switches both directions at once**, after checking that
  every entity in its profile exists and is reporting. A failed check changes
  nothing and says which entity failed (#1550: "keep the prior configuration on
  validation failure").
* **Only the pricing fields a profile owns are written.** Fees, P2P blocks,
  fixed-export schedules, settlement history and every other setting are left
  as they are. A profile without a price forecast array (Amber) clears the
  array, because the array would otherwise keep overriding the prices it is
  switching to.
* **Picking Manual changes nothing.** Prices stay as saved, editable in the
  Solver settings.
* **The upstream provider is never operated.** Amber Express's own
  pricing-mode select is read by its own sensors and is not touched here.

Tibber (#1582) is not detected yet: it needs a live payload sample first.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import pricing_autodetect as pa
from .const import (
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
)

MANUAL = "Manual"

# A profile maps each pricing field it owns to an entity_id, or to None where
# switching to it must CLEAR that field.
Profile = dict[str, str | None]


def detected_profiles(hass: HomeAssistant) -> dict[str, Profile]:
    """Label -> profile, for each provider whose profile is complete."""
    out: dict[str, Profile] = {}
    lv = pa.detect_localvolts_v2_profile(hass)
    if all(lv.get(field) for field in pa.LV_V2_PROFILE):
        out["LocalVolts v2"] = {field: lv[field] for field in pa.LV_V2_PROFILE}
    for label, found in (
        ("Amber Express", pa.detect_amber_express_profile(hass)),
        ("Amber Electric", pa.detect_amber_core_profile(hass)),
    ):
        imp = found.get(CONF_SOLVER_IMPORT_PRICE_SENSOR)
        exp = found.get(CONF_SOLVER_EXPORT_PRICE_SENSOR)
        if imp and exp:
            out[label] = {
                CONF_SOLVER_IMPORT_PRICE_SENSOR: imp,
                CONF_SOLVER_EXPORT_PRICE_SENSOR: exp,
                CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: None,
            }
    return out


def active_label(options: dict[str, Any], profiles: dict[str, Profile]) -> str:
    """The profile the saved options match, field for field, else Manual."""
    for label, profile in profiles.items():
        if all((options.get(f) or None) == v for f, v in profile.items()):
            return label
    return MANUAL


def problems(hass: HomeAssistant, profile: Profile) -> list[str]:
    """Why switching to `profile` would leave the Solver without prices, as
    plain sentences; empty when it is safe."""
    out = []
    for field, entity_id in profile.items():
        if entity_id is None:
            continue
        label = pa.PRICING_FIELD_LABELS.get(field, field)
        state = hass.states.get(entity_id)
        if state is None:
            out.append(f"{label}: {entity_id} does not exist")
        elif state.state in ("unavailable", "unknown"):
            out.append(f"{label}: {entity_id} is {state.state}")
    return out


def applied(options: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """`options` with the profile's fields set (None clears one)."""
    out = dict(options)
    for field, entity_id in profile.items():
        if entity_id is None:
            out.pop(field, None)
        else:
            out[field] = entity_id
    return out


def status(options: dict[str, Any], profiles: dict[str, Profile]) -> dict[str, Any]:
    """What the select shows as attributes: the active pricing entities and
    what each detected provider would set."""
    return {
        "import_price_sensor": options.get(CONF_SOLVER_IMPORT_PRICE_SENSOR),
        "export_price_sensor": options.get(CONF_SOLVER_EXPORT_PRICE_SENSOR),
        "price_forecast_array_sensor": options.get(
            CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR
        ),
        "detected_providers": {
            label: {pa.PRICING_FIELD_LABELS.get(f, f): v for f, v in profile.items()}
            for label, profile in profiles.items()
        },
    }
