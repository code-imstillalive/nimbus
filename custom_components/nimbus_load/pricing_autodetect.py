"""Find a LocalVolts v2 install and propose Nimbus's pricing profile from it
(nimbus #1550, LocalVolts first).

A household should not have to know which of LocalVolts v2's ~27 sensors feed
which Solver field. Picking them by hand is how a tester ended up with P2P
missing from his plan entirely: he set the P2P matched-rate sensor but not the
price forecast array, and without the array Nimbus prices on its generic path,
which does not read the matched rate at all.

What this does
--------------
* **Detects** a configured, loaded `localvolts_v2` config entry and resolves
  each sensor by its integration and its built-in unique_id,
  `<config entry id>_<key>` -- never by entity_id, which a household can
  rename (the reference household's own LV v2 sensors came up as
  `sensor.hallway_zone_...` after an area assignment and were renamed back).
* **Proposes, never overwrites.** The Solver settings steps pre-fill a field
  from this only when that field is empty. A field the household has set,
  to anything, is left exactly as it is.
* **Tells the household** at startup when LocalVolts v2 is installed and a
  profile field is still empty, and when a matched-rate sensor is set without
  a price forecast array (the trap above). It changes no configuration.
* **More than one LocalVolts v2 account** (several NMIs) is ambiguous: nothing
  is proposed, rather than guessing which site the Solver should plan.

The profile is the one the reference household and the LV v2 maintainer
agreed on #1550 (6 Oct 2026): five fields, each a distinct role. That is the
whole Basic contract.

#1537 adds one TRANSITIONAL binding outside it (`LV_V2_OPTIONAL_PROFILE`): the
second P2P matched-rate source, pre-filled with LV v2's Sell P2P Matched Cost
(`haeo_feed.py` key `sell_matched_cost`). It is a legacy binding for one Grid
pricing role, kept until the provider profile (#1574, PR #1585) carries both
projections of the matched rate itself. It is never counted as missing, never
notified about, and never needed to finish setup.

AEMO NEM Data (nimbus #1578)
----------------------------
`aemo_nem`'s regional 30-minute forecast is a WHOLESALE price. Nimbus reads
it in any price field (it is often a feed-in price, and with network fees a
buy price), but proposes it for one field only: the regional spot forecast,
which extends a retail feed past its own horizon. Whether a household's own
tariff passes spot through is not something Nimbus can see, so it never
pre-fills the import or export price with it. Same rules as above: detected by config entry and the entity's
built-in unique_id (`sensor.aemo_nem_<region>_current_30min_forecast`, set
once at creation, so a renamed entity is still found), pre-filled only where
the field is empty, never overwritten, and only when a price forecast array
is set (the one path that reads the field). With several regions configured, the
one matching the home's NEM region (the Companion App geocode) is proposed;
if that does not single one out, nothing is.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import sensor_discovery
from .const import (
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_FLAT_FEE_RATE,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_NETWORK_FEE_1_RATE,
    CONF_SOLVER_NETWORK_FEE_2_RATE,
    CONF_SOLVER_NETWORK_FEE_3_RATE,
    CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE,
    CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR,
    CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2,
    CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR,
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
    CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
)
from .price_intervals import AEMO_NEM_DOMAIN, AEMO_NEM_FORECAST_KEY

_LOGGER = logging.getLogger(__name__)

LV_V2_DOMAIN = "localvolts_v2"

# Solver field -> LocalVolts v2 sensor key (its unique_id suffix). Ordered as
# the household sees them: Grid Prices step, then Sources step.
LV_V2_PROFILE: dict[str, str] = {
    CONF_SOLVER_IMPORT_PRICE_SENSOR: "buy_flex_up",
    CONF_SOLVER_EXPORT_PRICE_SENSOR: "sell_flex_up",
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "flex_up_forecast",
    CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR: "current_sell_rate",
    CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR: "p2p_settlement_history",
}

# nimbus #1537: transitional, optional bindings. Pre-filled where empty like
# the profile above, but outside the Basic contract: not counted in the
# startup "fields are empty" notification, so an install never has to set one.
LV_V2_OPTIONAL_PROFILE: dict[str, str] = {
    CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2: "sell_matched_cost",
}

NOTIFY_DETECTED_ID = "nimbus_localvolts_v2_detected"
NOTIFY_AEMO_DETECTED_ID = "nimbus_aemo_nem_detected"
NOTIFY_P2P_TRAP_ID = "nimbus_p2p_matched_rate_without_price_array"
NOTIFY_MISSING_ID = "nimbus_pricing_entity_missing"
NOTIFY_FEES_DOUBLED_ID = "nimbus_fees_on_top_of_flex_up"

# Every fee Nimbus adds on top of the import price (solver_inputs/prices.py).
_FEE_RATE_KEYS = (
    CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE,
    CONF_SOLVER_NETWORK_FEE_1_RATE,
    CONF_SOLVER_NETWORK_FEE_2_RATE,
    CONF_SOLVER_NETWORK_FEE_3_RATE,
    CONF_SOLVER_FLAT_FEE_RATE,
)


def detect_localvolts_v2_profile(hass: HomeAssistant) -> dict[str, str]:
    """Solver field -> entity_id for every profile sensor this install's
    LocalVolts v2 actually provides, or {} if there is no single loaded
    LocalVolts v2 entry. A sensor an older LV v2 release does not have
    (Flex Up Forecast arrived in 2.8.0, Sell Flex Up in 2.9.0) is simply
    absent from the result; a disabled entity is skipped. Includes the
    transitional LV_V2_OPTIONAL_PROFILE bindings, for pre-fill only."""
    entries = [
        e
        for e in hass.config_entries.async_entries(LV_V2_DOMAIN)
        # ConfigEntryState.LOADED, compared by value so this module needs no
        # config_entries import (keeps the flow's unit-test stubs unchanged).
        if getattr(e.state, "value", e.state) == "loaded"
    ]
    if len(entries) != 1:
        if len(entries) > 1:
            _LOGGER.debug(
                "Nimbus: %d LocalVolts v2 entries; not proposing a pricing "
                "profile, the site is ambiguous",
                len(entries),
            )
        return {}
    entry_id = entries[0].entry_id
    registry = er.async_get(hass)
    found: dict[str, str] = {}
    for field, key in {**LV_V2_PROFILE, **LV_V2_OPTIONAL_PROFILE}.items():
        entity_id = registry.async_get_entity_id(
            "sensor", LV_V2_DOMAIN, f"{entry_id}_{key}"
        )
        if entity_id is None:
            continue
        reg_entry = registry.async_get(entity_id)
        if reg_entry is not None and reg_entry.disabled_by is not None:
            continue
        found[field] = entity_id
    return found


def detect_aemo_nem_forecast(hass: HomeAssistant) -> dict[str, str]:
    """{CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: entity_id} for the AEMO
    NEM Data regional 30-minute forecast this install should use, or {}.

    Candidates are the enabled `current_30min_forecast` sensors of every
    loaded `aemo_nem` entry, found through the entity registry by their
    built-in unique_id. One candidate is proposed as is. Several (one per
    configured region) are narrowed to the home's NEM region; anything
    still not unique proposes nothing rather than a guessed region."""
    registry = er.async_get(hass)
    candidates: dict[str, str] = {}  # region-bearing unique_id -> entity_id
    for entry in hass.config_entries.async_entries(AEMO_NEM_DOMAIN):
        if getattr(entry.state, "value", entry.state) != "loaded":
            continue
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
            uid = str(reg.unique_id or "")
            if (
                reg.domain == "sensor"
                and uid.endswith(f"_{AEMO_NEM_FORECAST_KEY}")
                and reg.disabled_by is None
            ):
                candidates[uid] = reg.entity_id
    if len(candidates) > 1:
        region, _ = sensor_discovery.resolve_geocoded_region_and_prefix(
            hass.states.async_all("sensor")
        )
        if region:
            tag = f"_{region.lower()}_"
            candidates = {u: e for u, e in candidates.items() if tag in u.lower()}
    if len(candidates) != 1:
        if candidates:
            _LOGGER.debug(
                "Nimbus: %d AEMO NEM Data regional forecasts and no single "
                "home region; not proposing one",
                len(candidates),
            )
        return {}
    return {CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: next(iter(candidates.values()))}


def missing_profile_entities(
    hass: HomeAssistant, options: dict[str, Any]
) -> dict[str, str]:
    """Solver field -> configured entity_id, for each pricing field that
    points at an entity this install does not have. Typically a value copied
    from another household's setup (the reference household's own writer
    sensors, e.g. `sensor.localvolts_price_forecast`), which Nimbus then
    reads as empty without a word."""
    return {
        field: value
        for field in (*LV_V2_PROFILE, *LV_V2_OPTIONAL_PROFILE)
        if isinstance(value := options.get(field), str)
        and value
        and hass.states.get(value) is None
    }


def with_detected_profile(
    hass: HomeAssistant, defaults: dict[str, Any]
) -> dict[str, Any]:
    """`defaults` with each profile field filled from the detected LocalVolts
    v2 profile where it is EMPTY, or points at an entity that does not exist
    on this install. A field set to a real entity, of any integration, is
    untouched. This only pre-fills the form; nothing is saved until the
    household submits it."""
    out = dict(defaults)
    missing = missing_profile_entities(hass, out)
    for field, entity_id in detect_localvolts_v2_profile(hass).items():
        if not out.get(field) or field in missing:
            out[field] = entity_id
    # nimbus #1578: only where empty, and only with a price forecast array
    # set -- that is the one path that reads this field. Whatever is already
    # set there (a PD7DAY forecast, another region, anything) stays.
    if out.get(CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR):
        for field, entity_id in detect_aemo_nem_forecast(hass).items():
            if not out.get(field):
                out[field] = entity_id
    return out


async def async_notify_pricing_setup(
    hass: HomeAssistant, options: dict[str, Any]
) -> None:
    """At startup, tell the household (a persistent notification, nothing
    changed) when LocalVolts v2 is installed but a profile field is empty,
    and when the P2P matched rate is set without a price forecast array."""
    detected = detect_localvolts_v2_profile(hass)
    missing = missing_profile_entities(hass, options)
    if missing:
        listed = ", ".join(f"`{eid}`" for eid in missing.values())
        _LOGGER.warning(
            "Nimbus Solver: pricing setting(s) point at entities that do not "
            "exist on this install: %s",
            listed,
        )
        await _notify(
            hass,
            NOTIFY_MISSING_ID,
            "Nimbus: a price sensor does not exist",
            f"Solver settings point at {listed}, which this install does not "
            "have, so Nimbus reads nothing from "
            + ("it" if len(missing) == 1 else "them")
            + ". Open Nimbus → Configure → Solver settings"
            + (
                "; the LocalVolts v2 sensors are pre-filled in their place."
                if detected
                else " and choose your own sensors."
            ),
        )
    # Only the Basic profile counts: an empty transitional binding
    # (LV_V2_OPTIONAL_PROFILE) is never reported as something to fill in.
    empty = [
        f
        for f in detected
        if f in LV_V2_PROFILE and not options.get(f) and f not in missing
    ]
    if empty:
        await _notify(
            hass,
            NOTIFY_DETECTED_ID,
            "Nimbus found LocalVolts v2",
            f"{len(empty)} of Nimbus's {len(LV_V2_PROFILE)} LocalVolts price "
            "fields are empty. Open Nimbus → Configure → Solver settings: the "
            "LocalVolts v2 sensors are pre-filled there for you to check and "
            "save. Nothing has been changed.",
        )
    aemo = (
        detect_aemo_nem_forecast(hass)
        if options.get(CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR)
        else {}
    )
    if aemo and not options.get(CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR):
        await _notify(
            hass,
            NOTIFY_AEMO_DETECTED_ID,
            "Nimbus found AEMO NEM Data",
            "Nimbus's regional spot forecast is empty, so prices past the end "
            "of your price forecast array are held at its last value. Open Nimbus → Configure → "
            "Solver settings: "
            f"`{aemo[CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR]}` is pre-filled "
            "there for you to check and save. It is the wholesale price, used "
            "here to extend your retail prices past their own forecast. Nothing "
            "has been changed.",
        )
    if (
        options.get(CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR)
        or options.get(CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2)
    ) and not options.get(CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR):
        _LOGGER.warning(
            "Nimbus Solver: a P2P matched-rate sensor is set but no price "
            "forecast array, so the matched rate is not read and P2P is not "
            "priced. Set Solver settings -> Price forecast array."
        )
        await _notify(
            hass,
            NOTIFY_P2P_TRAP_ID,
            "Nimbus: P2P is not being priced",
            "A P2P matched-rate sensor is set, but no price forecast array. "
            "Without the array Nimbus does not read the matched rate, so your "
            "plan shows no P2P premium. Open Nimbus → Configure → Solver "
            "settings and set the price forecast array"
            + (
                f" (LocalVolts v2: `{detected[CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR]}`)."
                if CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR in detected
                else "."
            ),
        )

    # nimbus issue #1564: LocalVolts' Buy Flex Up is already spot PLUS the
    # network layer (its own sensor says so, and measured on the reference
    # household 2026-10-06: Flex Up minus spot equalled that install's
    # configured network + flat fee to the cent in every settled interval,
    # 1.30 c midday, 7.50 c off-peak, 22.31 c peak). Nimbus adds its
    # network/flat fees on top of the import price, so with Flex Up as the
    # import sensor any non-zero fee is counted twice and the LP sees grid
    # energy dearer than it is. Notify only; the household decides.
    # nimbus #1574 stage 2: reported as a Home Assistant Repair
    # (setup_health.py), which clears itself once the fees are 0; the
    # notification this used to raise is dismissed instead.
    doubled = detect_fees_doubled(hass, options, detected=detected)
    if doubled is not None:
        _LOGGER.warning(
            "Nimbus Solver: the import price is LocalVolts v2 Buy Flex Up, "
            "which already includes network charges, and fees are also set "
            "(%s) -- they are counted twice",
            doubled[1],
        )
    await _dismiss(hass, NOTIFY_FEES_DOUBLED_ID)


def detect_fees_doubled(
    hass: HomeAssistant,
    options: dict[str, Any],
    *,
    detected: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """(Flex Up entity, listed fees) when the import price is the detected
    LocalVolts v2 Buy Flex Up and any network/flat fee is non-zero, else
    None (nimbus #1564). Pure apart from the registry read."""
    if detected is None:
        detected = detect_localvolts_v2_profile(hass)
    flex_up = detected.get(CONF_SOLVER_IMPORT_PRICE_SENSOR)
    if not flex_up or options.get(CONF_SOLVER_IMPORT_PRICE_SENSOR) != flex_up:
        return None
    fees = {k: _as_float(options.get(k)) for k in _FEE_RATE_KEYS}
    set_fees = {k: v for k, v in fees.items() if v > 0}
    if not set_fees:
        return None
    return flex_up, ", ".join(f"`{k}` = {v:g}" for k, v in set_fees.items())


async def _dismiss(hass: HomeAssistant, notification_id: str) -> None:
    try:
        await hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": notification_id},
        )
    except Exception:  # noqa: BLE001 -- a notification must never block setup
        _LOGGER.debug("Nimbus: could not dismiss notification %s", notification_id)


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _notify(
    hass: HomeAssistant, notification_id: str, title: str, message: str
) -> None:
    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {"notification_id": notification_id, "title": title, "message": message},
        )
    except Exception:  # noqa: BLE001 -- a notification must never block setup
        _LOGGER.debug("Nimbus: could not create notification %s", notification_id)
