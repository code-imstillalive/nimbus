"""Diagnostics platform for Nimbus.

Lets a household attach a real, structured dump to a bug report (Settings ->
Devices & Services -> Nimbus -> the three-dot menu -> Download diagnostics)
instead of copy-pasting entity states by hand -- this is exactly how several
real Nimbus bugs this project has fixed were actually found and reported
(e.g. the 2026-08-15 stale-persisted-model crash, the 2026-08-22 wizard-skip
gap Mark Purcell hit on his own fresh install).

TO_REDACT is deliberately empty, not omitted -- Nimbus has no external
service credentials, API keys, or tokens anywhere in its config (confirmed
throughout quality_scale.yaml's own reauthentication-flow/inject-websession
exemptions: it reads local HA sensor states only). Entity IDs are left
unredacted on purpose, matching the standard HA convention for this class
of integration -- they're needed for real debugging and aren't secrets.
Kept as an explicit empty tuple (not skipping async_redact_data entirely)
so a future config field that IS sensitive flows through the redaction
path automatically rather than needing this file retrofitted.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import NimbusConfigEntry

TO_REDACT: tuple[str, ...] = ()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NimbusConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a Nimbus hub config entry."""
    coordinators = entry.runtime_data

    subentries: list[dict[str, Any]] = []
    for subentry_id, coordinator in coordinators.items():
        subentry = coordinator.subentry
        data = coordinator.data or {}
        # Forecast point COUNT, not the raw array -- the array is already
        # live on the entity itself (small, redundant to duplicate here),
        # and this file exists to show HEALTH at a glance, not replicate
        # the forecast attribute.
        forecast = data.get("forecast") or []
        subentries.append(
            {
                "subentry_id": subentry_id,
                "subentry_type": subentry.subentry_type,
                "title": subentry.title,
                "config": async_redact_data(dict(subentry.data), TO_REDACT),
                "coordinator": {
                    "last_update_success": coordinator.last_update_success,
                    "mode": data.get("mode"),
                    "trained_at": data.get("trained_at"),
                    "training_points": data.get("training_points"),
                    "validation_mae": data.get("validation_mae"),
                    "validation_mase": data.get("validation_mase"),
                    "forecast_point_count": len(forecast),
                    "forecast_first_time": forecast[0]["time"] if forecast else None,
                    "forecast_last_time": forecast[-1]["time"] if forecast else None,
                },
            }
        )

    return {
        "entry": {
            "title": entry.title,
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "subentries": subentries,
    }
