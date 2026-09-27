"""Build one `nem-flex-telemetry` schema-v2.0 record from Nimbus's own
numbers (nimbus issue #495, Signals 6/7 of #489).

Pure stdlib, no Home Assistant and no other Nimbus module, for the same
reason `nem_region.py` and `sensor_discovery.py` are: the record shape is
the part worth testing directly, and a test that has to stand a whole
integration up to reach it tests the plumbing instead.

## What this module is responsible for, and what it is not

It owns the **contract**: which key, which unit, which sign, which range,
and what a field that has no Nimbus source is allowed to say. It owns
none of the measurement -- every figure arrives as an argument, already
resolved by `solver_writer.publish_flex_telemetry_record()` from the live
plan and from real recorder history.

`schema/telemetry.schema.json` is vendored in this repo (v0.94.371, pinned
to upstream `024f45c6`, provenance in `schema/PROVENANCE.md`) and declares
`additionalProperties: false` at the top level. So the record is built as
a closed dict and never as "the sensor's attributes minus the ones HA
added" -- one extra or misnamed key fails validation outright, and
`tests/test_495_flex_telemetry_record.py` validates what this function
returns against that file rather than against a reconstruction of it.

## The three measurement decisions, stated rather than implied

**1. `interval_start_utc` is the LAST COMPLETE 5-minute interval, not the
one in progress.** The record's own point is that its measured fields are
*period means*, and a mean over the 20 seconds of the current interval
that have elapsed when the solve tick fires is not a period mean -- it is
the point read this issue exists to replace. Upstream labels a point read
with the floored current interval; Nimbus labels a true mean with the
interval it actually covers. `last_complete_interval_start()` is that
rule, and it is what makes two consecutive records exactly 300 s apart on
`:00/:05/...` regardless of when within the interval the tick ran.

**The one-interval consequence, disclosed rather than smoothed over:**
the plan-derived fields (the shadow prices, the flex availability, each
asset's setpoint, the envelope limits) come from the live solve's own
period 0, which begins at or after the measured window closes. They are
not averages of the measured interval and are not claimed to be. This
matters less than it sounds because the plan's own period 0 is 15 minutes
on the default tiered grid (`build_tiered_grid()`), i.e. already coarser
than 5 minutes -- every 5-minute slot inside it reads the same value, so
there is no finer answer being discarded. It is still a real offset, and
a consumer comparing a shadow price against a measured kW in the same
record should know which window each belongs to.

**2. `naive_baseline_method` is `"subtraction"`, and the baseline is
`house load - solar`.** The enum is `["subtraction",
"haeo_counterfactual"]`; `nimbus_counterfactual` is not a member, and
@purcell-lab settled this on 2026-09-13 (*"Subtraction is fine"*),
re-confirmed on 2026-09-27 when a later comment reopened the same fork.
So: `subtraction`, and the subtraction is the same energy-balance
identity `_compute_flex_report_for_window()` already uses --
`net_import = load - solar - discharge + charge`, minus the battery
terms. That is *not* the same as upstream's own v0.3 baseline, which is
house load ALONE and therefore counts every kWh of self-consumed solar
as a loss (the `total_savings_aud: -5083.37` artefact #489 measured).
Crediting solar in the baseline is the half of this issue's own
"fixes three measurement problems" claim that survives the enum
constraint.

What it deliberately does NOT claim: that a single 5-minute record shows
a saving while the battery charges. It does not, and no battery-excluding
baseline can -- charging at that instant genuinely IS extra import, and
the saving lands later on discharge. The honest form of that check is a
whole-day one, which is where
`tests/test_495_flex_telemetry_record.py::TestTheBaselineNetsOutOverADay`
puts it.

**3. A field with no Nimbus source is `null`, never a plausible zero.**
`shadow_load_forecast_price` is the case: the schema's own description
sources it from HAEO's `sensor.load_forecast_limit_shadow_price`, a
shadow price on a load-forecast LIMIT. Nimbus has no such limit -- its
load forecast enters `power_balance_t` as a constant, so its own
derivative is definitionally `shadow_energy_price`, already in the
record. Publishing that same number twice under two names would tell a
cohort dashboard it has two independent measurements. `null` is permitted
by the schema (`"type": ["number", "null"]`) and is the true answer. Same
rule as `GridSignals.grid_import_headroom_unranged` (#496): a zero that
means two things is not a measurement.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

SCHEMA_VERSION = "2.0"

# The NEM dispatch interval the schema is keyed on. `interval_start_utc`
# covers [interval_start_utc, interval_start_utc + 5 minutes).
INTERVAL_SECONDS = 300

# @purcell-lab's own call, 2026-09-13 -- see this module's docstring for
# the full account of why it is not `nimbus_counterfactual`.
NAIVE_BASELINE_METHOD = "subtraction"

# `"minimum": -2.0, "maximum": 20.0` on every price field in the schema.
# Clamping is reported, never silent: a clamp means a real Nimbus figure
# was outside a range the cohort feed accepts, which is information about
# this install, not a rounding detail.
PRICE_MIN = -2.0
PRICE_MAX = 20.0

# `"enum": ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]`.
VALID_REGIONS = frozenset({"NSW1", "QLD1", "VIC1", "SA1", "TAS1"})

# `"pattern": "^[0-9]{3}$"`.
_POSTCODE_PREFIX_RE = re.compile(r"^[0-9]{3}$")

# `"enum": ["stationary_battery", "ev"]`. Every Nimbus battery emits
# `stationary_battery` today: `BatteryConfig` has no EV flag at all
# (checked, not assumed -- there is no `is_ev`/`vehicle`/`kind` field on
# it), and #495's own field table already gates the EV entries on #467
# stage 2. Guessing "ev" from a participant's name is the
# name-shaped inference @purcell-lab rejected on #768.
ASSET_KIND_STATIONARY = "stationary_battery"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Nimbus's own home pack is named "home" (`BatteryPlan.name`); the schema's
# own example uses `home_battery` for exactly that asset.
_HOME_BATTERY_NAME = "home"


@dataclass(frozen=True)
class RecordBuild:
    """The outcome of a build attempt.

    `record is None` with a `reason` rather than a raised exception,
    because "this install cannot produce a valid record yet" is an
    ordinary, expected state (no geocoded region configured, flex signals
    switched off) and not an error -- the publisher logs the reason and
    posts nothing, the same "None means not computed this cycle"
    convention `publish_flex_signals()`/`publish_offer_curve()` already
    use for `Plan.grid_signals`/`Plan.offer_curve_import`.
    """

    record: dict[str, Any] | None = None
    reason: str | None = None
    clamped_fields: tuple[str, ...] = field(default_factory=tuple)


def floor_to_interval(ts: datetime) -> datetime:
    """`ts` floored to the NEM 5-minute boundary, in UTC.

    Converts first and floors second: flooring a local-time value and
    converting afterwards is only equivalent for whole-5-minute UTC
    offsets, which is true of every NEM region today and is exactly the
    kind of convention this repo has already been bitten by relying on
    (the `gt.hour` P2P-window bug in the standalone writer's own
    `_as_local()` comment).
    """
    utc = ts.astimezone(UTC)
    return utc.replace(
        minute=(utc.minute // 5) * 5,
        second=0,
        microsecond=0,
    )


def last_complete_interval_start(ts: datetime) -> datetime:
    """The start of the most recent interval that has fully elapsed --
    decision 1 in this module's docstring."""
    return floor_to_interval(ts) - timedelta(seconds=INTERVAL_SECONDS)


def format_interval_start(dt: datetime) -> str:
    """`"2026-09-27T04:35:00Z"` -- the schema's own
    `format: date-time` with the `Z` suffix its examples use, not
    `+00:00`, which is what `datetime.isoformat()` produces."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def asset_id_for(name: str) -> str:
    """A schema `asset_id` slug from a Nimbus battery participant name."""
    if name == _HOME_BATTERY_NAME:
        return "home_battery"
    slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_")
    return slug or "battery"


def _clamp_price(name: str, value: float | None, clamped: list[str]) -> float | None:
    """A price into the schema's `[-2, 20]`, recording that it happened.

    `None` passes through untouched: the five `shadow_*` fields are
    nullable and a null is a real answer (decision 3), so coercing it to
    a number here would be the exact missing-vs-zero conflation this
    module refuses everywhere else.
    """
    if value is None:
        return None
    numeric = float(value)
    if math.isnan(numeric):  # never a price, and json.dumps emits NaN raw
        clamped.append(name)
        return None
    if numeric < PRICE_MIN:
        clamped.append(name)
        return PRICE_MIN
    if numeric > PRICE_MAX:
        clamped.append(name)
        return PRICE_MAX
    return numeric


def _nonneg(value: float) -> float:
    """A schema `minimum: 0` field. Real measured power can read slightly
    negative on a sensor that should never be (a solar CT at night), and
    the schema rejects it -- clamping to 0.0 is the same posture
    `_compute_flex_report_for_window()` already takes on the same three
    sensors (`max(0.0, v * scale)`)."""
    numeric = float(value)
    if math.isnan(numeric) or numeric < 0.0:
        return 0.0
    return numeric


def build_asset(
    *,
    name: str,
    capacity_kwh: float,
    soc_kwh: float,
    charge_kw: float,
    discharge_kw: float,
    max_discharge_kw: float,
    available_up_kw: float,
    available_down_kw: float,
    shadow_power_balance_price: float | None,
    clamped: list[str],
) -> dict[str, Any]:
    """One `assets[]` entry.

    `setpoint_kw` is signed with the schema's convention (*"Positive =
    charging, negative = discharging"*), which is the opposite of this
    household's own `sensor.logger_battery_power` and is therefore stated
    here rather than inferred: `charge_kw - discharge_kw`, straight off
    the LP's own two non-negative variables, so no sign-convention
    setting is involved at all.

    `bidirectional_capable` is derived, not configured: a
    `max_discharge_kw` of 0 is a charge-only asset by the only definition
    the LP has.
    """
    capacity = _nonneg(capacity_kwh)
    soc_pct = 0.0 if capacity <= 0.0 else 100.0 * _nonneg(soc_kwh) / capacity
    return {
        "asset_id": asset_id_for(name),
        "kind": ASSET_KIND_STATIONARY,
        "bidirectional_capable": float(max_discharge_kw) > 0.0,
        "capacity_kwh": round(capacity, 3),
        "soc_pct": round(min(100.0, max(0.0, soc_pct)), 3),
        "setpoint_kw": round(float(charge_kw) - float(discharge_kw), 3),
        "available_up_kw": round(_nonneg(available_up_kw), 3),
        "available_down_kw": round(_nonneg(available_down_kw), 3),
        "shadow_power_balance_price": _round_or_none(
            _clamp_price(
                f"assets[{asset_id_for(name)}].shadow_power_balance_price",
                shadow_power_balance_price,
                clamped,
            ),
            4,
        ),
    }


def _round_or_none(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def build_record(
    *,
    interval_start: datetime,
    region: str | None,
    postcode_prefix: str | None,
    net_import_kw: float,
    solar_kw: float,
    house_load_kw: float,
    deferrable_load_kw: float,
    naive_baseline_kw: float,
    price_signal_seen: float,
    price_export_seen: float,
    envelope_import_limit_kw: float,
    envelope_export_limit_kw: float,
    flex_available_up_kw: float,
    flex_available_down_kw: float,
    shadow_energy_price: float | None,
    shadow_solar_forecast_price: float | None,
    shadow_envelope_import_price: float | None,
    shadow_envelope_export_price: float | None,
    assets: list[dict[str, Any]],
    deferrable_loads: list[dict[str, Any]] | None = None,
    clamped_in: list[str] | None = None,
) -> RecordBuild:
    """One schema-v2.0 record, or a `reason` it could not be built.

    `shadow_load_forecast_price` is deliberately absent from this
    signature: it is always `null` and there is no Nimbus figure a caller
    could pass. Taking it as an argument would invite someone to pass
    `shadow_energy_price` for it, which is precisely the duplication
    decision 3 rejects.
    """
    if region not in VALID_REGIONS:
        return RecordBuild(
            reason=(
                f"region {region!r} is not one of {sorted(VALID_REGIONS)} -- "
                "Nimbus resolves it from the Companion App's own "
                "sensor.<device>_geocoded_location (see sensor_discovery."
                "resolve_geocoded_region_and_prefix); exactly one usable "
                "such sensor is needed"
            )
        )
    if not (postcode_prefix and _POSTCODE_PREFIX_RE.match(postcode_prefix)):
        return RecordBuild(
            reason=(
                f"postcode_prefix {postcode_prefix!r} is not three digits -- "
                "same geocoded-location source as region above"
            )
        )
    # Seeded, not started fresh: `build_asset()` clamps each asset's own
    # `shadow_power_balance_price` against the same `[-2, 20]` range and
    # runs BEFORE this call, so dropping its findings here would report a
    # clean record that had in fact been clamped.
    clamped: list[str] = list(clamped_in or [])
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "interval_start_utc": format_interval_start(interval_start),
        "region": region,
        "postcode_prefix": postcode_prefix,
        "net_import_kw": round(float(net_import_kw), 3),
        "solar_kw": round(_nonneg(solar_kw), 3),
        "house_load_kw": round(_nonneg(house_load_kw), 3),
        "deferrable_load_kw": round(_nonneg(deferrable_load_kw), 3),
        "naive_baseline_kw": round(float(naive_baseline_kw), 3),
        "naive_baseline_method": NAIVE_BASELINE_METHOD,
        # Not nullable in the schema, unlike the five shadow prices below
        # -- a clamp here is the only thing standing between a real
        # negative-FiT spike and a rejected record.
        "price_signal_seen": _clamp_price(
            "price_signal_seen", price_signal_seen, clamped
        ),
        "price_export_seen": _clamp_price(
            "price_export_seen", price_export_seen, clamped
        ),
        "envelope_import_limit_kw": round(_nonneg(envelope_import_limit_kw), 3),
        "envelope_export_limit_kw": round(_nonneg(envelope_export_limit_kw), 3),
        "flex_available_up_kw": round(_nonneg(flex_available_up_kw), 3),
        "flex_available_down_kw": round(_nonneg(flex_available_down_kw), 3),
        "shadow_energy_price": _round_or_none(
            _clamp_price("shadow_energy_price", shadow_energy_price, clamped), 4
        ),
        # Always null -- decision 3 in this module's docstring.
        "shadow_load_forecast_price": None,
        "shadow_solar_forecast_price": _round_or_none(
            _clamp_price(
                "shadow_solar_forecast_price", shadow_solar_forecast_price, clamped
            ),
            4,
        ),
        "shadow_envelope_import_price": _round_or_none(
            _clamp_price(
                "shadow_envelope_import_price", shadow_envelope_import_price, clamped
            ),
            4,
        ),
        "shadow_envelope_export_price": _round_or_none(
            _clamp_price(
                "shadow_envelope_export_price", shadow_envelope_export_price, clamped
            ),
            4,
        ),
        "assets": list(assets),
        # `{"type": "object", "additionalProperties": true}` items, and
        # the schema's own description says *"Empty for v0.3.0 default
        # install. Schema slot reserved for v0.4."* -- so `[]` validates
        # and is what #495's field table asks for until #476 lands.
        "deferrable_loads": list(deferrable_loads or []),
    }
    # A price that came back `None` from the clamp is a NaN that was
    # rejected. The two non-nullable price fields cannot carry that, so
    # the record is refused rather than emitted invalid.
    for key in ("price_signal_seen", "price_export_seen"):
        if record[key] is None:
            return RecordBuild(
                reason=(
                    f"{key} is not a finite number -- the schema requires a "
                    "number here and Nimbus will not emit a record it knows "
                    "is invalid"
                ),
                clamped_fields=tuple(clamped),
            )
    return RecordBuild(record=record, clamped_fields=tuple(clamped))
