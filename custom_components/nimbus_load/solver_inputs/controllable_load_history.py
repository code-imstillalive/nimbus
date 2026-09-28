"""What each Controllable Load ACTUALLY delivered, hour by hour, on an
already-elapsed day -- reconstructed from its own power sensor's recorder
history.

nimbus issue #768 (Mark Purcell). That issue asked for two things: score
the whole battery fleet, and let the oracle answer *"could we have
optimised better by moving a controllable load's own timing?"* The first
half shipped in v0.94.268. The second half has been blocked since
2026-09-13 on one capability the issue named and nothing provided:

    Reconstructing real historical controllable-load delivery is itself a
    new capability. Today's `delivered_today_kwh` is a live intraday
    counter (`load_run_state.py`), not something queryable for an
    arbitrary past scoring day.

This module is that capability, and deliberately ONLY that. Mark's own
sequencing decision, 2026-09-27:

    the historical controllable-load delivery reconstruction is the real
    prerequisite and gates everything else -- don't wire `adequacy_loads=`
    / `sheddable_loads=` into `compute_quality_report()`'s oracle call
    until that reconstruction exists, since without it there's no
    real-delivered number to hand the oracle. Build that first, land it
    as its own change, then wire the LP plumbing ... as a second, smaller
    step.

So nothing here reaches the oracle. `compute_quality_report()`'s own
`build_plan()` call is untouched, every EPR and regret figure is
bit-identical to before (pinned by
`tests/test_768_delivery_is_published_without_moving_the_score.py`), and
the reconstruction is PUBLISHED as a measurement so it can be checked
against a real install before anything is scored against it. The wiring
is issue #1357. That is the same order #1242 used for
`soc_discrepancy_power_coverage`: ship the measurement, not the
adjustment.

**The `target_kwh` convention is already decided and this module
implements it.** Mark, 2026-09-27: *"real-delivered kWh, not the
configured target"* -- because configured-target treats "the household
set the wrong target" as outside the oracle's own error, while
real-delivered isolates the pure timing question. What this module
returns is therefore the REAL delivered energy, per period, and never
the configured `deferrable_target_kwh`.

## Why this is small, and what it is NOT allowed to do cheaply

Two pieces the issue listed as missing arrived for unrelated reasons:

- `fetch_entity_power_history_kw()` (#843) -- real recorded power over an
  arbitrary window, normalised to kW **per row** from each sample's own
  recorded unit. Built for the EV pack sensor that reports W for ~80 s on
  wake.
- power-sensor auto-discovery (#768's own prerequisite, v0.94.292) -- a
  Controllable Load resolves its own `device_class: power` sibling off the
  device registry, so `controllable_load_power_sensor` need not be set.

What genuinely remained was the integration step, and it has one real
trap. `resample_history_mean()` carries the last sample forward
indefinitely -- correct for a STATE, energy-fabricating for POWER. A
naive sum over recorder rows would credit `power x (a huge gap)` for a
restart or a long unavailable stretch, on precisely the days most worth
scoring. `load_run_state.py` has refused exactly that since it was
written (`MAX_SAMPLE_GAP_HOURS = 1.0`), and #1161 taught the
battery-participant reconstruction the same guard via
`_stale_power_period_indices()`. This module reuses that same helper and
that same constant rather than restating the number -- retyping `1.0`
here is how the live counter and the scorer would drift apart again.

## Honest absence, not a confident zero

A load with no resolvable power sensor, or no recorded rows at all inside
the window, is reported as **not scorable** with a reason -- never as
`delivered_kwh = 0.0`. Days before the sensor existed genuinely cannot be
reconstructed, and the issue already named the posture: the same
`null`-not-`0` convention `offered_up_kwh` takes on a day the switch was
off. A zero would read as "this load did nothing", which is a different
and checkable claim.

Native/in-process mode ONLY, same reasoning and same graceful `[]`
fallback as every other subentry reader here -- a standalone/cron
deployment has no subentries at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see this package's `__init__.py` for the full reasoning."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer  # type: ignore[no-redef]
    return solver_writer


# Reasons a load is not scorable for a window. Published verbatim, so a
# household reads a cause rather than inferring one from a missing value.
REASON_NO_POWER_SENSOR = "no_power_sensor"
REASON_NO_HISTORY = "no_history"
REASON_OK = "reconstructed"


@dataclass(frozen=True)
class ControllableLoadDelivery:
    """One Controllable Load's real, already-elapsed delivery over the
    scored window.

    `delivered_kwh_by_period` is the series the oracle will be handed
    once the LP plumbing is wired (the second step of Mark's own
    sequencing above); `delivered_kwh` is its total, and the only part
    published today.
    """

    subentry_id: str
    name: str
    kind: str
    power_sensor: str | None
    scorable: bool
    reason: str
    delivered_kwh: float | None = None
    delivered_kwh_by_period: tuple[float, ...] = ()
    sample_count: int = 0
    stale_period_count: int = 0
    n_periods: int = 0

    @property
    def observed_period_fraction(self) -> float | None:
        """Share of the window whose delivery came from a real reading
        rather than being zeroed as stale.

        The direct analogue of `soc_discrepancy_power_coverage` (#1181):
        the reconstruction is conservative where telemetry is missing, so
        this is how much of it is actually evidence.
        """
        if not self.scorable or self.n_periods <= 0:
            return None
        return round(1.0 - (self.stale_period_count / self.n_periods), 4)

    def as_attribute(self) -> dict:
        """The compact, publishable summary -- deliberately NOT the
        per-period series.

        A 288-period array per load would put this one field over the
        recorder's own 16 KB per-state attribute cap (#944) on a
        three-load install, and nothing consumes a published copy: the
        oracle will read `delivered_kwh_by_period` in process.
        """
        return {
            "subentry_id": self.subentry_id,
            "name": self.name,
            "kind": self.kind,
            "power_sensor": self.power_sensor,
            "scorable": self.scorable,
            "reason": self.reason,
            "delivered_kwh": (
                None if self.delivered_kwh is None else round(self.delivered_kwh, 4)
            ),
            "observed_period_fraction": self.observed_period_fraction,
            "sample_count": self.sample_count,
        }


def integrate_delivered_kwh(
    power_kw_by_period: list[float],
    period_hours: float,
    stale_period_indices: set[int] | frozenset[int],
) -> tuple[tuple[float, ...], float]:
    """Per-period kWh and its total, crediting nothing for a period whose
    power reading was only held forward across a recorder gap.

    Pure arithmetic, no Home Assistant and no numpy -- separated from the
    fetch so the guard itself can be tested against a hand-written series
    rather than only through a mocked recorder.

    Negative power is clamped to 0.0. A Controllable Load is a consumer;
    a negative reading on its own power sensor is a measurement artifact
    (a CT clamp on backwards, a shared circuit), and subtracting it from
    delivered energy would report a load as having generated. #768's own
    manual reconstruction hit exactly this on a real sensor -- "a real
    -17.48 kWh of negative-signed readings ... which don't fit any known
    real discharge mechanism" -- and excluded them.
    """
    out: list[float] = []
    for i, kw in enumerate(power_kw_by_period):
        if i in stale_period_indices:
            out.append(0.0)
            continue
        out.append(max(0.0, float(kw)) * period_hours)
    return tuple(out), float(sum(out))


def resolve_controllable_load_delivery_history(
    *,
    day_start: datetime,
    day_end: datetime,
    grid_times: list[datetime],
    period_hours: float,
    n_periods: int,
) -> list[ControllableLoadDelivery]:
    """One `ControllableLoadDelivery` per configured `controllable_load`
    subentry, scorable or not.

    Every configured load appears in the result, including the ones that
    cannot be reconstructed -- a caller counting "how many of my loads
    does this day's score know about" must be able to see the ones it
    does not, which is the distinction `scored_participants` already
    makes on the battery side.

    Reads `subentry.data` directly rather than through
    `_resolve_controllable_load_tuning()`. That overlay applies the
    LIVE values of the seven tunable `number.*` entities, which describe
    the load's configuration NOW -- meaningless, and quietly wrong, for a
    day that has already elapsed. Nothing in this reconstruction depends
    on a tunable field anyway: it reads recorded power, not a target.
    """
    sw = _solver_writer()
    if sw._NATIVE_HASS is None:
        return []
    try:
        from ..const import (
            CONF_CONTROLLABLE_LOAD_KIND,
            CONF_CONTROLLABLE_LOAD_NAME,
            DOMAIN,
            SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        )
    except ImportError:  # pragma: no cover - standalone/cron path
        from const import (  # type: ignore[no-redef]
            CONF_CONTROLLABLE_LOAD_KIND,
            CONF_CONTROLLABLE_LOAD_NAME,
            DOMAIN,
            SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        )

    entries = sw._NATIVE_HASS.config_entries.async_entries(DOMAIN)
    if not entries:
        return []

    out: list[ControllableLoadDelivery] = []
    for subentry in entries[0].subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_CONTROLLABLE_LOAD:
            continue
        data = subentry.data
        name = str(data.get(CONF_CONTROLLABLE_LOAD_NAME) or subentry.subentry_id)
        kind = str(data.get(CONF_CONTROLLABLE_LOAD_KIND) or "")
        power_sensor = sw.resolve_controllable_load_power_sensor(data)
        if not power_sensor:
            solver_shared._LOGGER.info(
                "Nimbus quality: controllable load '%s' has no power sensor "
                "(none configured, and none discoverable on its own device) "
                "-- its real delivery for %s is not reconstructable, so it "
                "is reported as not scorable rather than as zero delivery",
                name,
                day_start.date().isoformat(),
            )
            out.append(
                ControllableLoadDelivery(
                    subentry_id=subentry.subentry_id,
                    name=name,
                    kind=kind,
                    power_sensor=None,
                    scorable=False,
                    reason=REASON_NO_POWER_SENSOR,
                    n_periods=n_periods,
                )
            )
            continue

        # Per-row unit scaling, scoped to exactly this one fetch (#843) --
        # the same reason the battery-participant reconstruction uses this
        # function rather than the cheaper attribute-stripped read.
        power_hist = sw.fetch_entity_power_history_kw(power_sensor, day_start, day_end)
        if not power_hist:
            solver_shared._LOGGER.info(
                "Nimbus quality: controllable load '%s' has no recorded rows "
                "for %s on %s -- not scorable for this day (a day before the "
                "sensor existed genuinely cannot be reconstructed)",
                name,
                power_sensor,
                day_start.date().isoformat(),
            )
            out.append(
                ControllableLoadDelivery(
                    subentry_id=subentry.subentry_id,
                    name=name,
                    kind=kind,
                    power_sensor=power_sensor,
                    scorable=False,
                    reason=REASON_NO_HISTORY,
                    n_periods=n_periods,
                )
            )
            continue

        per_period_kw = list(
            sw.resample_history_mean(power_hist, grid_times, period_hours)
        )
        # #1161's guard, via the same helper and the same
        # load_run_state.MAX_SAMPLE_GAP_HOURS default the participant
        # reconstruction uses. Not restated here on purpose.
        stale = set(
            sw._stale_power_period_indices(power_hist, grid_times, period_hours)
        )
        by_period, total = integrate_delivered_kwh(per_period_kw, period_hours, stale)
        if stale:
            solver_shared._LOGGER.info(
                "Nimbus quality: controllable load '%s' had no %s samples for "
                "%d of %d periods on %s -- those periods contribute no "
                "delivered energy (see observed_period_fraction)",
                name,
                power_sensor,
                len(stale),
                n_periods,
                day_start.date().isoformat(),
            )
        out.append(
            ControllableLoadDelivery(
                subentry_id=subentry.subentry_id,
                name=name,
                kind=kind,
                power_sensor=power_sensor,
                scorable=True,
                reason=REASON_OK,
                delivered_kwh=total,
                delivered_kwh_by_period=by_period,
                sample_count=len(power_hist),
                stale_period_count=len(stale),
                n_periods=n_periods,
            )
        )
    return out


# nimbus issue #1357: the oracle side. Everything above reconstructs what the
# loads REALLY delivered; this turns that into the LP configs the scorer's
# perfect-foresight oracle needs in order to re-time them.

ORACLE_SKIP_THERMAL = "kind=thermal is not scored (nimbus #768)"
ORACLE_SKIP_UNKNOWN_KIND = "unrecognised kind"
ORACLE_SKIP_NO_ENERGY = "no delivered energy reconstructed for this window"
ORACLE_SKIP_NO_WINDOW = "deferrable window did not resolve for this window"
ORACLE_SKIP_GONE = "subentry no longer configured"


def _np():
    import numpy as np

    return np


def build_oracle_controllable_loads(
    *,
    deliveries,
    grid_times,
    period_hours,
    n_periods: int,
    window_start,
):
    """The `OracleControllableLoads` bundle for one scored window.

    Turns the reconstructions above into `AdequacyLoadConfig` /
    `SheddableLoadConfig` objects, plus the per-period kWh the caller must
    remove from the oracle's base load. Returns `None` when there is nothing to
    score at all, which is the value that leaves `compute_quality_report()` on
    its pre-#1357 path exactly.

    ## Why the config-reading lives here and not in `solver/`

    `solver/quality_report.py` is the pure package and knows nothing about
    subentries, `kind=`, or which loads are scorable; it takes already-built
    configs. This module already iterates the same subentries for the
    reconstruction and already carries the `const` imports, so a second reader
    would be a duplicate of the first.

    ## `target_kwh` is the REAL delivered energy, never the configured target

    Issue #768's own decision. A day where the tank only got half its target is
    a day the oracle must be scored against having delivered half. Charging the
    oracle with the configured target would grade it against work the house
    never did, and make every such day look like a dispatch failure.

    ## The same-day-in-progress correction is deliberately NOT applied

    `build_controllable_loads()` calls `_earliest_period_for_same_day_window()`
    because the forward plan's `now` can sit INSIDE the window and a load
    cannot be scheduled into the past. A scored window has entirely elapsed and
    `window_start` is its beginning, so there is no in-progress case to correct
    for -- applying that correction here would move the window for a reason
    that does not exist retrospectively.

    ## Honest skips

    Every configured load that does not enter the oracle is reported with a
    reason rather than silently dropped: `kind=thermal` (excluded per #768 -- a
    different LP shape, a hard state-variable constraint, and a sample size of
    one to validate against), an unscorable reconstruction (no power sensor, or
    a day before that sensor existed), a window that does not resolve, and a
    load whose reconstruction found no energy at all. Same posture
    `scored_participants` takes on the battery side, and the reason a
    partially-scored day cannot read as a wholly-scored one.
    """
    sw = _solver_writer()
    from solver.elements import (
        AdequacyLoadConfig,
        SheddableLoadConfig,
    )
    from solver.quality_report import (
        OracleControllableLoads,
    )

    try:
        from ..const import (
            CONF_DEFERRABLE_DEADLINE_HOUR,
            CONF_DEFERRABLE_EARLIEST_HOUR,
            CONF_DEFERRABLE_MAX_POWER_KW,
            CONF_DEFERRABLE_SHORTFALL_PRICE,
            CONF_SHEDDABLE_MIN_FRACTION,
            CONF_SHEDDABLE_SHED_COST,
            DOMAIN,
            SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        )
    except ImportError:  # pragma: no cover - standalone/cron path
        from const import (  # type: ignore[no-redef]
            CONF_DEFERRABLE_DEADLINE_HOUR,
            CONF_DEFERRABLE_EARLIEST_HOUR,
            CONF_DEFERRABLE_MAX_POWER_KW,
            CONF_DEFERRABLE_SHORTFALL_PRICE,
            CONF_SHEDDABLE_MIN_FRACTION,
            CONF_SHEDDABLE_SHED_COST,
            DOMAIN,
            SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        )

    if not deliveries or sw._NATIVE_HASS is None:
        return None
    entries = sw._NATIVE_HASS.config_entries.async_entries(DOMAIN)
    if not entries:
        return None
    by_id = {
        se.subentry_id: se.data
        for se in entries[0].subentries.values()
        if se.subentry_type == SUBENTRY_TYPE_CONTROLLABLE_LOAD
    }

    adequacy = []
    sheddable = []
    skipped = []
    total_kwh = [0.0] * n_periods

    for d in deliveries:
        if d.kind == "thermal":
            skipped.append((d.name, ORACLE_SKIP_THERMAL))
            continue
        if not d.scorable:
            skipped.append((d.name, d.reason))
            continue
        if not d.delivered_kwh or d.delivered_kwh <= 0.0:
            skipped.append((d.name, ORACLE_SKIP_NO_ENERGY))
            continue
        data = by_id.get(d.subentry_id)
        if data is None:  # pragma: no cover - the subentry vanished mid-cycle
            skipped.append((d.name, ORACLE_SKIP_GONE))
            continue
        by_period = list(d.delivered_kwh_by_period) or [0.0] * n_periods

        if d.kind == "sheddable":
            # forecast_kw is the load's OWN reconstructed profile -- what it
            # really drew, period by period. The LP may shed any of it at
            # shed_cost, which is the freedom the household actually has.
            profile_kw = [
                (kwh / h if h > 0 else 0.0)
                for kwh, h in zip(by_period, period_hours, strict=False)
            ]
            sheddable.append(
                SheddableLoadConfig(
                    name=d.name,
                    forecast_kw=_np().asarray(profile_kw, dtype=float),
                    shed_cost=float(data.get(CONF_SHEDDABLE_SHED_COST) or 0.0),
                    min_fraction=float(data.get(CONF_SHEDDABLE_MIN_FRACTION) or 0.0),
                    subentry_id=d.subentry_id,
                )
            )
        elif d.kind == "deferrable":
            earliest_hour = data.get(CONF_DEFERRABLE_EARLIEST_HOUR)
            deadline_hour = data.get(CONF_DEFERRABLE_DEADLINE_HOUR)
            earliest_period = (
                sw._resolve_hour_to_period_index(
                    grid_times, window_start, float(earliest_hour), is_deadline=False
                )
                if earliest_hour is not None
                else 0
            )
            deadline_period = (
                sw._resolve_hour_to_period_index(
                    grid_times, window_start, float(deadline_hour), is_deadline=True
                )
                if deadline_hour is not None
                else n_periods - 1
            )
            if deadline_period < earliest_period:
                skipped.append((d.name, ORACLE_SKIP_NO_WINDOW))
                continue
            adequacy.append(
                AdequacyLoadConfig(
                    name=d.name,
                    max_power_kw=float(data.get(CONF_DEFERRABLE_MAX_POWER_KW) or 0.0),
                    # #768: real delivered, never the configured target.
                    target_kwh=float(d.delivered_kwh),
                    earliest_period=earliest_period,
                    deadline_period=deadline_period,
                    shortfall_price=float(
                        data.get(CONF_DEFERRABLE_SHORTFALL_PRICE) or 0.0
                    ),
                    subentry_id=d.subentry_id,
                )
            )
        else:
            skipped.append((d.name, ORACLE_SKIP_UNKNOWN_KIND))
            continue

        for i, kwh in enumerate(by_period[:n_periods]):
            total_kwh[i] += float(kwh)

    if not adequacy and not sheddable:
        # A bundle carrying skips only, so the report still says which loads it
        # did not score. `__post_init__` requires no kWh array when there are
        # no loads -- passing one would remove energy the LP never gets back.
        return OracleControllableLoads(skipped=tuple(skipped))
    return OracleControllableLoads(
        adequacy=tuple(adequacy),
        sheddable=tuple(sheddable),
        delivered_kwh_by_period=_np().asarray(total_kwh, dtype=float),
        skipped=tuple(skipped),
    )
