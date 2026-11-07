"""Price forecast reading, blending and banding, moved from `solver_writer.py`
(#2289 item 6, step 8 part 2).

Resamples a retailer or AEMO price forecast onto the solve grid, extends its
tails from AEMO and history, measures the 5-minute retail offset, bands
prices by percentile and warns once on stale or disagreeing sources. Called
by `solver_inputs/prices.py`, `solver_reports/quality.py` and
`solver_writer` through this module. Shared helpers (`ha_get`, `NATIVE`,
`parse_iso`, `_LOGGER`, ...) are reached through `solver_shared` by module,
so a test patches `solver_shared.<name>`; this module adds no late-import
seam back into `solver_writer`.
"""

from __future__ import annotations

import json
import statistics
import urllib.error
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

try:
    from .. import price_intervals
except ImportError:  # pragma: no cover - standalone/cron path
    import price_intervals  # type: ignore[no-redef]
try:
    from ..ml.blend import cross_source_spread
except ImportError:  # pragma: no cover - standalone/cron path
    from ml.blend import cross_source_spread  # type: ignore[no-redef]

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]


def fetch_price_history(entity_id: str, days: int = 5) -> list[tuple[datetime, float]]:
    """Real recorded history for a single sensor's numeric state, as
    (local time, value) points -- the shared building block for
    compute_5min_offset() below. Returns [] (callers fall back further)
    if history is genuinely unavailable -- must never crash the writer.

    nimbus issue #363 (Mark Purcell, codebase review), finding 4: this
    used to duplicate ~90% of solver_shared.fetch_entity_history_range() below --
    same REST/native dual-mode recorder bridge, same point-building
    loop, differing only in "last N days from now" vs an explicit
    window and cosmetic details (a bare try/float() vs an explicit
    unknown/unavailable check beforehand, which skip the exact same
    values either way; start/end already being UTC-aware here makes
    solver_shared.fetch_entity_history_range()'s own .astimezone(UTC) call a no-op).
    Now a thin wrapper -- one real implementation, not two kept in sync
    by hand. See tests/test_fetch_price_history_and_entity_history_
    range.py for the differential coverage confirming this is
    behaviour-preserving.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return solver_shared.fetch_entity_history_range(entity_id, start, end)


def resample_generic_price_forecast(
    entity_id: str, grid_times: list[datetime]
) -> list[float] | None:
    """Generic {time, value}-shaped forecast resampler for the portable
    FALLBACK price path below (2026-08-22, real ask from Mark Purcell's
    own install: his configured price sensors ARE real, live, genuinely
    dynamic Amber Electric sensors -- not a hardcoded placeholder -- but
    the BASE amberelectric price sensor carries no forecast attribute at
    all; Amber only exposes forward-looking prices via a separate
    service call (amberelectric.get_forecasts), which needs its own
    small helper sensor to expose as a real attribute -- see this
    project's docs/real-world-integration/files/
    amber_forecast_for_solver.yaml for a real, working example any
    Amber-using installer can drop in).

    Works for ANY price sensor whose own `forecast` attribute is a list
    of {"time": <iso datetime string>, "value": <float>} dicts -- the
    same simple convention this project's own LocalVolts sensors already
    use (see resample_price_with_extrapolation()'s own real-world
    equivalent above), so this isn't Amber-specific despite the
    motivating case -- any future portable price source that produces
    this same shape gets picked up automatically, no config-flow change
    needed. This household's own has_price_forecast_array branch never reaches
    this function -- it exists purely for the portable fallback path,
    kept in sync with the sibling standalone script.

    Step ("hold the most recent point") lookup, deliberately NOT linear
    interpolation between two forecast points -- smearing a genuine
    price step into a fake ramp is a real, already-documented bug class
    in this project's own history (HAEO's own forecast_fuser, see
    CLAUDE.md's session-41-era investigation) and Amber's own forecasts
    are themselves already step-shaped (each interval duplicated at its
    own start_time and end_time, both carrying the same value -- see the
    real amber_forecast_for_solver.yaml companion file).

    Returns None (caller falls back to the flat current-value repeat) if
    the entity has no `forecast` attribute, it's empty, or nothing in it
    parses -- never raises. A period at or before the earliest available
    forecast point uses that point's own value (best available), rather
    than leaving a real gap.

    Also accepts `calibrated` as a fallback key when `value` is absent
    (2026-08-25, direct household ask to blend in Mark Purcell's own
    NEM PD7 sensor, sensor.nem_pd7day_qld1_nem_spot_price_forecast --
    see fetch_aemo_forecast()'s own docstring for the full "why
    calibrated, not raw_value" story: NEM PD7 runs a real isotonic-
    regression spike-calibration algorithm against historical AEMO
    forecast accuracy, exactly the kind of "commercial grade algorithm"
    a plain hold-flat/linear-extrapolation fallback isn't). Before this,
    pointing a new solver_import/export_price_sensor_2/_3 field directly
    at NEM PD7 silently produced zero usable points -- every one of its
    forecast entries carries `calibrated`/`raw_value`/`spike_credible`,
    never a bare `value` key, so the strict-`value` parse below dropped
    every single point without error. `value` is still tried FIRST and
    preferred where present -- this only widens what counts as usable,
    it never changes what an existing Amber-shaped sensor resolves to.
    """
    r = resample_generic_price_forecast_with_coverage(entity_id, grid_times)
    return r[0] if r is not None else None


def _price_scale_of_state(state: dict) -> float:
    """nimbus #1537: the factor taking a price sensor's values to $/kWh, from
    the `unit_of_measurement` of a state already read (see
    price_intervals.price_unit_scale)."""
    attrs = state.get("attributes") if isinstance(state, dict) else None
    return price_intervals.price_unit_scale((attrs or {}).get("unit_of_measurement"))


def price_now(entity_id: str, fallback: float = 0.0, *, quiet: bool = False) -> float:
    """A price sensor's current state in $/kWh, scaled by its own unit (nimbus
    #1537 -- a c/kWh sensor used to be read 100x too high). One read of the
    entity, as solver_shared.safe_num() makes; an unreadable state goes through solver_shared.safe_num()
    for its warning and fallback (`fallback` is in $/kWh). `quiet` is for a
    reader that carries on without the value: no warning, a debug line."""
    try:
        state = solver_shared.ha_get(entity_id)
    except Exception as err:  # noqa: BLE001 -- solver_shared.safe_num() decides, as it always has
        solver_shared._LOGGER.debug(
            "price_now(%s): read failed (%s); via solver_shared.safe_num()",
            entity_id,
            err,
        )
        return solver_shared.safe_num(entity_id, fallback=fallback, quiet=quiet)
    scale = _price_scale_of_state(state)
    try:
        return solver_shared.finite_float(state["state"]) * scale
    except (KeyError, TypeError, ValueError):
        return (
            solver_shared.safe_num(entity_id, fallback=fallback / scale, quiet=quiet)
            * scale
        )


def _openadr_forecast_rows(entity_id: str) -> list | None:
    """nimbus #1583: an OpenADR 3 VEN price sensor's full forecast rows, from
    its `openadr3_ven.get_forecast` action (the sensor publishes only a
    summary). None on any failure -- the caller then treats the sensor as
    having no forecast, as for any other unavailable source."""
    return price_intervals.openadr_rows_from_response(
        # A literal domain, so the DC-R15 inventory can see this is the
        # read-only OpenADR forecast lookup, not an equipment command.
        solver_shared.ha_call_service_with_response(
            "openadr3_ven", "get_forecast", {"entity_id": entity_id}
        ),
        entity_id,
    )


def resample_generic_price_forecast_with_coverage(
    entity_id: str, grid_times: list[datetime]
) -> tuple[list[float], list[bool]] | None:
    """`_resample_generic_price_forecast_raw()` in $/kWh: its values scaled by
    the sensor's own `unit_of_measurement` (nimbus #1537), from the same single
    read of the entity. See that function's docstring for the resampling.

    nimbus #1623 (IV&V pass): a provider-shape sensor (`price_intervals.
    is_provider_shape()` -- AEMO/PD7DAY/Amber/OpenADR, nimbus #1550) is read by
    `_resample_generic_price_forecast_raw()`'s own provider-adapter branch,
    which already returns $/kWh -- each adapter (`aemo_nem_intervals()`,
    `pd7day_intervals()`, `amber_core_intervals()`, `amber_express_intervals()`,
    `openadr_intervals()`) reads the provider's own forecast row field directly
    as the $/kWh value, with no reference to `unit_of_measurement` at all.
    Applying `_price_scale_of_state()` on top of that -- derived purely from
    the entity's declared `unit_of_measurement`, which for a provider sensor
    describes its own *current-state* basis (AEMO's native wholesale price is
    genuinely quoted in $/MWh) and has no bearing on the already-$/kWh
    forecast rows -- would silently corrupt an already-correct price by up to
    1000x. So a provider-shape sensor skips this scale entirely."""
    try:
        state = solver_shared.ha_get(entity_id)
    except Exception as err:  # noqa: BLE001 -- the raw reader logs and returns None
        solver_shared._LOGGER.debug(
            "price forecast %s: read failed (%s); raw reader retries", entity_id, err
        )
        return _resample_generic_price_forecast_raw(entity_id, grid_times)
    r = _resample_generic_price_forecast_raw(entity_id, grid_times, state)
    attrs = state.get("attributes") or {}
    if r is None or price_intervals.is_provider_shape(attrs):
        return r
    scale = _price_scale_of_state(state)
    if scale == 1.0:
        return r
    values, real = r
    return [v * scale for v in values], real


def _resample_generic_price_forecast_raw(
    entity_id: str, grid_times: list[datetime], state: dict | None = None
) -> tuple[list[float], list[bool]] | None:
    """Same resampling as resample_generic_price_forecast() (see that
    function's own docstring for the full "why a generic {time,value}
    resampler" story), plus a per-period REAL-coverage mask alongside
    the values.

    `real_mask[i]` is True only when grid_times[i] falls within this
    source's own [first real point, last real point] span -- False for
    any period where the "hold the most recent point" step-lookup above
    had nothing real to hold yet (before the source's first point) or
    is repeating its last point flat because the source's own forecast
    doesn't reach that far. Extracted as its own function (2026-08-27,
    nimbus repo issue #216, Mark Purcell) specifically so
    blend_price_with_secondary_sources() can tell a genuine second
    opinion apart from a source silently repeating its own boundary
    value -- see that function's own docstring for why the distinction
    matters. resample_generic_price_forecast() itself just discards the
    mask, unchanged for every other existing caller.
    """
    try:
        state = solver_shared.ha_get(entity_id) if state is None else state
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace solver_shared._LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        # nimbus issue #363 (Mark Purcell, codebase review): fallback
        # stays, breadcrumb added.
        solver_shared._LOGGER.debug(
            "Nimbus Solver: resample_generic_price_forecast_with_coverage(%s) failed",
            entity_id,
            exc_info=True,
        )
        return None
    attrs = state.get("attributes") or {}
    if price_intervals.is_provider_shape(attrs):  # nimbus #1550 adapters
        return price_intervals.provider_forecast_on_grid(
            attrs, grid_times, lambda: _openadr_forecast_rows(entity_id)
        )
    forecast = attrs.get("forecast")
    if not forecast:
        return None
    points: list[tuple[datetime, float]] = []
    for f in forecast:
        try:
            t = solver_shared.parse_iso(f["time"])
            raw = f["value"] if "value" in f else f["calibrated"]
            v = float(raw)
        except (KeyError, TypeError, ValueError):
            continue
        points.append((t, v))
    if not points:
        return None
    points.sort(key=lambda p: p[0])
    first_t, last_t = points[0][0], points[-1][0]
    result: list[float] = []
    real_mask: list[bool] = []
    for gt in grid_times:
        candidates = [v for t, v in points if t <= gt]
        result.append(candidates[-1] if candidates else points[0][1])
        real_mask.append(first_t <= gt <= last_t)
    return result, real_mask


def blend_price_with_secondary_sources(
    primary: list[float],
    cfg: dict,
    secondary_keys: tuple[str, str],
    grid_times: list[datetime],
    primary_real_mask: list[bool] | None = None,
) -> tuple[list[float], NDArray[np.float64] | None, list[str]]:
    """Optional second/third price source blending (2026-08-25, direct
    household ask: "u also are missing my blended price forecasts...
    in case we can feed it more than one... e.g. aemo... and amber").

    `primary` is whatever spot_import_raw/spot_export either the
    has_price_forecast_array or generic branch above already produced -- this
    function never re-derives it, only optionally blends more sources
    in on top. `secondary_keys` is a (key_2, key_3) pair of cfg keys
    (e.g. ("solver_import_price_sensor_2", "solver_import_price_sensor_3"))
    so the same function serves both import and export without
    duplicating the fetch/blend loop.

    Fetches each configured secondary source via
    resample_generic_price_forecast_with_coverage() (the same generic
    resampler the portable fallback branch already uses -- also accepts
    NEM PD7's own `calibrated` field, see that function's own
    docstring), and returns cross_source_spread() as a real, earned
    disagreement-based uncertainty signal (not an arbitrary knob) for
    the caller to feed into price_risk_aversion's own band, exactly as
    solar's own multi-source spread widens its confidence band.

    COVERAGE-AWARE (2026-08-27, nimbus repo issue #216, Mark Purcell:
    "Solver export_price is a ~0.5x linear compression of the Amber
    Express feed-in sensor"). Previously blended every configured
    source at a flat, unconditional equal weight across the WHOLE grid,
    including periods where a source's own real forecast doesn't reach
    yet -- resample_generic_price_forecast() silently holds a source's
    own first/last real point flat for any grid time outside its real
    coverage, and the old blend treated that placeholder exactly like a
    genuine second opinion, diluting a fully real primary value 50/50
    with a source that was really just repeating its own boundary
    number. Root-caused live: Mark's `_sensor_2` (a "day 2-7" AEMO
    forecast with no real data for today at all) was blended 50/50
    against a fully-real Amber Express primary for the ENTIRE captured
    window, producing exactly the reported ~0.5x + constant-offset
    compression -- confirmed by his own follow-up test clearing
    `_sensor_2` and getting an exact 1:1 pass-through (R²=1.0000).

    PRIMARY-PREFERRING (2026-08-27, nimbus repo issue #239, following
    #236: "50/50 price-blend inflates export_price... producing
    simultaneous 22kW grid import + 30kW grid export"). Previously, once
    coverage-awareness landed for #216, a period where BOTH the primary
    and a secondary had real coverage still averaged them 50/50 -- fine
    when the two sources roughly agree, but a real Amber Express tick
    and a real QLD1 PD7DAY forecast can legitimately disagree by tens of
    cents on the same instant (they're not independent estimators of the
    same thing, see #239's own market-structure argument), and averaging
    them can fabricate a price no counterparty will ever settle at --
    confirmed live inverting the import/export relationship and causing
    an unphysical simultaneous-import-and-export LP plan.

    Now the primary (via `primary_real_mask` -- callers without one
    default to "always real", i.e. always wins) is used UNBLENDED
    whenever it has real coverage at a period, full stop -- never
    averaged with a secondary just because one happens to be live too.
    A secondary only ever contributes where the PRIMARY lacks real
    coverage (extending the horizon past Amber's own ~24h reach, its one
    genuine job): a period where only a secondary has real data passes
    that source through unchanged (still matches Mark's own
    clear-`_sensor_2` finding from #216); the rare period where NOTHING
    has real coverage anywhere still falls back to the old equal-weight
    blend across every source's own held-flat value (the honest
    "everyone's guessing" case) rather than an arbitrary pick.

    Returns `(primary, None, ["primary"] * len(primary))` completely
    unchanged whenever no secondary source is configured OR configured
    but currently unavailable -- a single-source install (the
    overwhelming majority today) is byte-identical to before this
    function existed (aside from the trivial, always-"primary" label
    list, new in #631).

    Note on period 0 (2026-08-27, nimbus repo issue #220, Mark Purcell:
    "Settled prices must not be blended"): grid_times[0] is always "now"
    by construction (build_tiered_grid()), and every configured price
    source publishes a SETTLED, contractual value for that block, not
    an estimate -- there is no valid blending weight for it other than
    1.0 on the primary. This function still runs its normal coverage
    logic on period 0 like any other (it's a general-purpose blend, not
    aware of which index is "now"); the caller (main()) is responsible
    for re-asserting the settled primary value at index 0 AFTER calling
    this, which is simpler and keeps this function's own contract
    (and its existing direct unit tests) unchanged. The caller must
    likewise force this function's own `source_labels[0]` back to
    "primary" after that re-assertion, for the same reason.

    nimbus issue #631 (Mark Purcell, live finding: a single 71.3 kWh
    charge block committed 20 hours ahead on a secondary source's own
    price, with nothing in the published plan distinguishing "known
    from the retailer's own near-term forecast" from "extrapolated from
    a weekly tariff table" -- `import_price`/`import_price_raw` were
    byte-identical in every row regardless of which source actually won
    that period). The third return value is a per-period label,
    `"primary"`/`"secondary"`/`"fallback"`, naming EXACTLY which branch
    of the same decision this function already makes for `blended[i]`
    produced that period's own value -- not a second, independently
    reasoned classification that could ever drift out of sync with the
    real blend decision above it.
    """
    sources = [np.array(primary, dtype=float)]
    real_masks: list[list[bool]] = [
        list(primary_real_mask)
        if primary_real_mask is not None
        else [True] * len(primary)
    ]
    for key in secondary_keys:
        entity_id = cfg.get(key)
        if entity_id:
            r = resample_generic_price_forecast_with_coverage(entity_id, grid_times)
            if r is not None:
                fc, mask = r
                sources.append(np.array(fc, dtype=float))
                real_masks.append(mask)
    if len(sources) == 1:
        return primary, None, ["primary"] * len(primary)
    n = len(primary)
    blended = np.empty(n, dtype=float)
    source_labels: list[str] = []
    for i in range(n):
        # Primary-preferring (nimbus #239): where the primary source has real
        # coverage, it is used unblended. On a NEM tariff import and export
        # both derive from the same spot price, so averaging a live primary
        # with a secondary forecast only corrupts it: a 50/50 blend once
        # inverted import and export, and the LP planned 22 kW import with
        # 30 kW export. Secondaries still extend the horizon past the
        # primary's coverage, and where nothing is real every source's held
        # value is averaged (#216).
        if real_masks[0][i]:
            blended[i] = sources[0][i]
            source_labels.append("primary")
            continue
        secondary_real_idx = [j for j in range(1, len(sources)) if real_masks[j][i]]
        idx = secondary_real_idx if secondary_real_idx else list(range(len(sources)))
        blended[i] = float(np.mean([sources[j][i] for j in idx]))
        source_labels.append("secondary" if secondary_real_idx else "fallback")
    return list(blended), cross_source_spread(sources), source_labels


def fetch_aemo_forecast(
    sensor_id: str | None = None,
) -> list[tuple[datetime, float]]:
    """Real, FORWARD-looking AEMO NEM spot price forecast (a
    purcell-lab/nem_pd7day-shaped sensor) -- covers the FULL 96h horizon
    on a real QLD1 install (confirmed live 2026-08-16: 367 real 30-min
    points, now -> +7.6 days), unlike LocalVolts (~24h real) or Amber
    (~23.7h real, confirmed via
    sensor.amber_express_116kathouse_forecast_horizon itself, not
    guessed). This is the only real price source on this system with
    genuine coverage all the way to the end of a 96h horizon, so it's
    the anchor for anything beyond LV's own real coverage.

    `sensor_id` is CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR (const.py's
    own comment has the full real-bug-audit story, 2026-09-02): the
    same NEM-region-forecast CONCEPT is genuinely useful outside QLD1
    (any Australian state running the same nem_pd7day integration for
    their own region), but the entity name itself is per-region --
    hardcoding QLD1 made this permanently unreachable for anyone else.
    None/blank (the default, and the only behaviour for a market with
    no NEM-equivalent forecast at all) hits the exact same "unavailable"
    fallback below as it always has.

    Uses the 'calibrated' field, NOT 'raw_value' -- real, direct finding
    (2026-08-16, user: "that is why mark written nem pd7 to take out
    these false aemo predictions ... 95% never happen"). Each forecast
    point carries BOTH: raw_value is AEMO's own uncalibrated spot
    forecast, which can predict extreme, low-probability spike events
    that mostly don't materialize (confirmed live: raw_value=8.999 for
    2026-08-19T19:00, 78h out, while the SAME point's own
    calibrated=0.105355 and spike_credible=False -- an explicit flag
    this integration computes specifically to say "don't trust the raw
    spike"). 'calibrated' (isotonic-regression corrected against real
    n_obs=41 historical observations at that horizon) is the field this
    integration exists to provide; using raw_value here would have fed
    the Solver a false, uncredible price signal.

    Only 30-min resolution (AEMO's own forecast granularity) -- the
    real 5-min-of-day structure comes from compute_5min_offset() below,
    layered on top of this coarser forward anchor. Returns [] (caller
    falls back further) if unavailable -- must never crash the writer.
    nimbus #1578: AEMO NEM Data rows too, see price_intervals.
    """
    if not sensor_id:
        return []
    try:
        fc = solver_shared.ha_get(sensor_id)["attributes"]["forecast"]
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return []
    points = price_intervals.regional_step_points(fc)  # incl. a PD7DAY tariff
    if points is not None:
        return points
    return sorted(
        (
            (solver_shared.parse_iso(p["time"]), p["calibrated"])
            for p in fc
            if p.get("calibrated") is not None
        ),
        key=lambda x: x[0],
    )


def extend_generic_price_tails(
    cfg: dict,
    grid_times: list[datetime],
    import_side: tuple[list[float], list[bool], bool],
    export_side: tuple[list[float], list[bool], bool],
) -> tuple[list[float], list[float]]:
    """nimbus #1537 item 4: the generic price branch's tail past each
    source's own last real point, priced as regional wholesale forecast plus
    that source's learned 5-minute-of-day retail markup -- the extension the
    price-array branch has had since 2026-08-16 (see compute_5min_offset()).

    Each side is (values, real_mask, has_forecast). Only periods AFTER the
    side's last real period change; a gap inside the coverage is left as
    the reader held it. Returns both value lists, unchanged unless the
    regional spot forecast sensor, the regional current-price sensor and
    that side's own recorded history all exist -- an install without them
    keeps today's flat hold. A markup bucket with no history uses the mean
    markup. History is scaled by the sensor's own unit (#1537 item 1)."""
    regional = cfg.get("solver_regional_spot_forecast_sensor")
    current = cfg.get("solver_regional_spot_current_price_sensor")
    if not regional or not current:
        return import_side[0], export_side[0]
    aemo = fetch_aemo_forecast(regional)
    if not aemo:
        return import_side[0], export_side[0]
    out = []
    for key, (values, real, has_fc) in (
        ("solver_import_price_sensor", import_side),
        ("solver_export_price_sensor", export_side),
    ):
        last = max((i for i, r in enumerate(real) if r), default=None)
        if not has_fc or last is None or last >= len(values) - 1:
            out.append(values)
            continue
        scale = price_unit_scale_of_history(cfg[key])
        history = [(t, v * scale) for t, v in fetch_price_history(cfg[key])]
        offset = compute_5min_offset(history, regional_spot_sensor=current)
        if not offset:
            out.append(values)
            continue
        mean = sum(offset.values()) / len(offset)
        extended = list(values)
        for i in range(last + 1, len(values)):
            wholesale = None
            for t, v in aemo:
                if t <= grid_times[i]:
                    wholesale = v
                else:
                    break
            if wholesale is None:
                continue
            bucket = (
                solver_shared._local(grid_times[i]).hour * 12
                + solver_shared._local(grid_times[i]).minute // 5
            )
            extended[i] = float(wholesale + offset.get(bucket, mean))
        out.append(extended)
    return out[0], out[1]


def price_unit_scale_of_history(entity_id: str) -> float:
    """The unit factor for `entity_id`'s recorded history (nimbus #1537):
    the sensor's current unit, as HA records the state in it."""
    try:
        return _price_scale_of_state(solver_shared.ha_get(entity_id))
    except Exception as err:  # noqa: BLE001 -- unknown unit reads as before, x1
        solver_shared._LOGGER.debug(
            "price history unit %s: read failed (%s); x1", entity_id, err
        )
        return 1.0


def compute_5min_offset(
    real_history: list[tuple[datetime, float]],
    days: int = 5,
    regional_spot_sensor: str | None = None,
    same_poll_wholesale: dict[int, tuple[datetime, float]] | None = None,
    wholesale_scale: Callable[[datetime], float | None] | None = None,
) -> dict[int, float]:
    """Real, empirical (retail price - regional wholesale spot) offset,
    binned by 5-MINUTE-of-day (288 buckets: hour*12 + minute//5) from
    real, multi-day RECORDED history for both sides -- not a single
    forecast snapshot.

    Real finding chain (2026-08-16, direct ask: "amber and lv are both
    5min measured intervals" / "the pricing should be per 5min intervals
    not per hour"): a single flat offset (computed from one day's
    forecast overlap) ranged +$0.0055 to +$0.24 -- useless. Binning that
    SAME single-day data by HOUR narrowed it (e.g. hour 18:
    0.2362-0.2403) but hour 16 still spanned 0.017-0.248 -- a real
    artifact of using one forecast snapshot, not a genuine repeating
    intra-hour pattern. Re-checked against 5 real days of actual
    RECORDED history (`regional_spot_sensor` -- CONF_SOLVER_REGIONAL_
    SPOT_CURRENT_PRICE_SENSOR, was hardcoded to this household's own
    sensor.aemo_nem_qld1_current_5min_period_price until the 2026-09-02
    audit -- vs sensor.costsflexup/earningsflexup, both genuinely 5-min
    resolution) binned at 5-min-of-day: every one of the 12 buckets
    inside that same 16:00-17:00 hour is now tight across all 5 real
    days (e.g. 16:00: 0.2267-0.2710) -- confirms the earlier hourly
    smear was a single-snapshot artifact, and that this household's
    real retail markup pattern genuinely has fine, repeatable 5-min-
    level structure worth capturing rather than averaging away.

    Falls back to an empty dict (caller then falls back further) if
    `regional_spot_sensor` is blank, or either side's real history is
    unavailable -- must never crash the writer. `wholesale_scale(start)`
    (#1786) scales each wholesale first (loss factors, GST); None skips it.
    """
    if not real_history:
        return {}
    wholesale = _wholesale_by_interval(regional_spot_sensor, days, same_poll_wholesale)
    if not wholesale:
        return {}

    # nimbus #1694: each interval's SETTLED retail price against the SAME
    # interval's wholesale (the old "at or before" pairing read the previous
    # interval, provisional posts included), and each bucket's median over its
    # last _OFFSET_RECENT_DAYS samples, so a tariff change is followed.
    retail = _settled_by_interval(real_history)
    by_bucket: dict[int, list[tuple[datetime, float]]] = {}
    for key in retail.keys() & wholesale.keys():
        start, real_v = retail[key]
        scale = 1.0 if wholesale_scale is None else wholesale_scale(start)
        if scale is None:
            continue
        bucket = (
            solver_shared._local(start).hour * 12
            + solver_shared._local(start).minute // 5
        )
        fee = real_v - wholesale[key][1] * scale
        by_bucket.setdefault(bucket, []).append((start, fee))
    return {
        b: statistics.median(v for _, v in sorted(rows)[-_OFFSET_RECENT_DAYS:])
        for b, rows in by_bucket.items()
    }


def _wholesale_by_interval(
    regional_spot_sensor: str | None,
    days: int,
    same_poll_wholesale: dict[int, tuple[datetime, float]] | None,
) -> dict[int, tuple[datetime, float]]:
    """The regional AEMO sensor's settled value per interval, overlaid with
    nimbus #1698's same-poll wholesale: where the retailer's own response
    carried the interval's wholesale (Amber Express `spot_per_kwh` / 1.10),
    it wins for that interval, as it shares the retail price's poll and so has
    no cross-sensor timing (a late AEMO post keyed into the wrong interval)."""
    aemo_history = (
        fetch_price_history(regional_spot_sensor, days=days)
        if regional_spot_sensor
        else []
    )
    wholesale = _settled_by_interval(aemo_history)
    wholesale.update(same_poll_wholesale or {})
    return wholesale


# nimbus #1694: one sample per 5-minute-of-day bucket per day, so the median of
# the last three days -- robust to one glitched interval, and a changed tariff
# takes over after two days.
_OFFSET_RECENT_DAYS = 3


def _settled_by_interval(
    history: list[tuple[datetime, float]],
) -> dict[int, tuple[datetime, float]]:
    """nimbus #1694: the LAST value recorded in each 5-minute interval, keyed
    by interval, with the interval's start. A retailer's provisional first
    post (LocalVolts, ~3 s in) gives way to the settled one (~20 s in)."""
    out: dict[int, tuple[datetime, float]] = {}
    for t, v in sorted(history, key=lambda p: p[0]):
        key = int(t.timestamp()) // 300
        out[key] = (datetime.fromtimestamp(key * 300, t.tzinfo), float(v))
    return out


def check_aemo_p5min_disagreement(
    regional_spot_sensor: str | None,
    retail_price_now: float,
    bucket_5min: int,
    offset_by_5min: dict[int, float],
    threshold_dollars: float,
) -> dict | None:
    """nimbus issue #452 (Mark Purcell): cross-check the current
    interval's real retail commodity price against AEMO's own live
    P5MIN wholesale price (`regional_spot_sensor` -- CONF_SOLVER_
    REGIONAL_SPOT_CURRENT_PRICE_SENSOR, already the exact entity this
    check needs -- confirmed live against a real household's own
    `sensor.aemo_nem_qld1_current_5min_period_price`, no new sensor
    field required).

    A raw retail-vs-wholesale difference is EXPECTED (network fees,
    retailer margin) -- comparing against zero would flag every single
    period. What's genuinely anomalous is a difference from the
    household's OWN typical same-time-of-day markup, which compute_
    5min_offset() above already computes as a real, bucketed empirical
    mean from recorded history. This reuses that same `offset_by_5min`
    dict (already computed by every caller of this function for a
    different purpose -- see main()'s own call site) rather than
    re-deriving a second notion of "expected offset".

    Deliberately scoped to the CURRENT interval only, not "current +
    next" as #452's own title suggests -- the only two real AEMO
    entities confirmed live for this (Mark Purcell, 2026-09-13) are a
    genuine 5-min CURRENT price and a 30-min-bucketed FORWARD forecast,
    neither of which is a true "next 5-min interval" P5MIN value; the
    current-interval check alone already covers this issue's own core
    motivation ("exactly the window where a dispatch decision is most
    consequential"). A next-interval check is real, honest follow-up
    work, not silently dropped -- see #452's own tracking comment.

    Returns None (no flag, no data to check) when `regional_spot_sensor`
    is blank, this bucket has no offset history yet (compute_5min_
    offset()'s own empty-dict/missing-bucket case), or the live AEMO
    read fails/is non-numeric (`unavailable`/`unknown` state, entity
    gone) -- never raises, matches this file's "must never break the
    real solve" convention throughout (see solver_shared.ha_get()'s own callers
    elsewhere in this file for the same defensive pattern).
    """
    if not regional_spot_sensor:
        return None
    expected_offset = offset_by_5min.get(bucket_5min)
    if expected_offset is None:
        return None
    try:
        aemo_now = solver_shared.finite_float(
            solver_shared.ha_get(regional_spot_sensor)["state"]
        )
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None
    expected_retail = aemo_now + expected_offset
    disagreement = retail_price_now - expected_retail
    return {
        "aemo_p5min_now": round(aemo_now, 4),
        "retail_now": round(retail_price_now, 4),
        "expected_retail": round(expected_retail, 4),
        "disagreement_dollars": round(disagreement, 4),
        "threshold_dollars": round(threshold_dollars, 4),
        "flagged": abs(disagreement) > threshold_dollars,
    }


# nimbus issue #452: log-once-per-PERIOD discipline (not the open-ended
# log-once-per-condition-until-recovery pattern _ENVELOPE_LIMIT_WARNED
# uses above) -- a genuine price disagreement is a fresh, time-varying
# event every 5-min bucket, not a persistent degraded state, so it must
# re-fire each time "now" rolls into a new flagged period rather than
# staying silent forever after the first one. A single remembered
# timestamp (not a growing set) is enough: solves re-run every ~1 minute
# on the SAME period far more often than periods actually change.
_AEMO_P5MIN_LAST_WARNED_PERIOD: datetime | None = None


# nimbus #1793: one warning per stale episode, keyed on the source's stamp.
_STALE_PRICE_LAST_WARNED: str | None = None


def _warn_stale_price_source_once(detail: dict) -> None:
    global _STALE_PRICE_LAST_WARNED
    if _STALE_PRICE_LAST_WARNED == detail.get("source_updated"):
        return
    _STALE_PRICE_LAST_WARNED = detail.get("source_updated")
    solver_shared._LOGGER.warning(
        "Nimbus #1793: price source %s was last rebuilt at %s (%.0f min ago, "
        "over %.0f); planning on AEMO's forecast plus the learned markup "
        "until it refreshes (logged once per stale episode)",
        detail.get("entity_id"),
        detail.get("source_updated"),
        detail.get("age_minutes", 0.0),
        detail.get("stale_after_minutes", 0.0),
    )


def _warn_aemo_p5min_disagreement_once(period_start: datetime, detail: dict) -> None:
    global _AEMO_P5MIN_LAST_WARNED_PERIOD
    if _AEMO_P5MIN_LAST_WARNED_PERIOD == period_start:
        return
    _AEMO_P5MIN_LAST_WARNED_PERIOD = period_start
    solver_shared._LOGGER.warning(
        "Nimbus #452: current-interval retail price ($%.4f/kWh) disagrees "
        "with AEMO P5MIN ($%.4f/kWh, expected ~$%.4f/kWh given this time-"
        "of-day's normal retail markup) by $%.4f/kWh, beyond the "
        "configured $%.4f/kWh threshold (logged once per period)",
        detail["retail_now"],
        detail["aemo_p5min_now"],
        detail["expected_retail"],
        detail["disagreement_dollars"],
        detail["threshold_dollars"],
    )


# nimbus issue #452: same log-once-per-PERIOD discipline as the
# current-interval check above -- a forecast/actuals divergence is a
# fresh event each window, not a persistent degraded state, so it must
# re-fire when "now" rolls into a new period rather than going silent
# forever after the first one.
_AEMO_FC_LAST_WARNED_PERIOD: datetime | None = None


def _warn_aemo_forecast_vs_actuals_once(period_start: datetime, detail: dict) -> None:
    global _AEMO_FC_LAST_WARNED_PERIOD
    if _AEMO_FC_LAST_WARNED_PERIOD == period_start:
        return
    _AEMO_FC_LAST_WARNED_PERIOD = period_start
    solver_shared._LOGGER.warning(
        "Nimbus #452: AEMO's own 30-min forecast ($%.4f/kWh) for %s-%s "
        "disagrees with its own realised 5-min prices so far ($%.4f/kWh "
        "from %d sample(s)) by $%.4f/kWh, beyond the configured "
        "$%.4f/kWh threshold (logged once per period)",
        detail["forecast_price"],
        detail["window_start"],
        detail["window_end"],
        detail["realised_mean_price"],
        detail["n_samples"],
        detail["disagreement_dollars"],
        detail["threshold_dollars"],
    )


def compute_price_percentile_band(
    price_history: list[tuple[datetime, float]], percentile: float
) -> dict[int, float]:
    """Real, empirical price band by 5-MINUTE-of-day (288 buckets), from
    real multi-day recorded history -- same bucketing technique as
    compute_5min_offset() above, reused rather than re-derived. Builds
    GridConfig.import_price_upper/export_price_lower for price_risk_
    aversion (2026-08-21 -- see this project's own network.py docstring,
    "PRICE-FORECAST-ERROR HEDGING" section, and the direct household ask
    that drove it: "the forecasts are always wrong but they tend to be
    more expensive in the afternoons, so waiting is not a good idea...
    a flexible sliding charging urgency control").

    `percentile` in [0, 100] -- pass a HIGH percentile (e.g. 90) to build
    a pessimistic UPPER bound for import price (real historical evidence
    of how bad this time-of-day's price can genuinely get), a LOW
    percentile (e.g. 10) to build a pessimistic LOWER bound for export
    price. No pre-clamping against the point forecast needed here --
    network.py's own _risk_adjusted_one_sided() already guarantees a
    bound worse than the point forecast is a no-op (np.maximum(0.0, ...)),
    never inverts the adjustment -- confirmed by its own committed test,
    test_bound_worse_than_forecast_is_ignored_not_inverted.

    Falls back to an empty dict (caller then leaves price_risk_aversion
    as a complete no-op for whichever periods have no bucket -- see
    resample_price_with_extrapolation()'s own "hold flat" precedent) if
    history is unavailable -- must never crash the writer.
    """
    if not price_history:
        return {}
    by_bucket: dict[int, list[float]] = {}
    for t, v in price_history:
        bucket = solver_shared._local(t).hour * 12 + solver_shared._local(t).minute // 5
        by_bucket.setdefault(bucket, []).append(v)
    return {b: float(np.percentile(vals, percentile)) for b, vals in by_bucket.items()}


def apply_price_band(
    point_price: list[float], grid_times: list[datetime], band_by_5min: dict[int, float]
) -> list[float] | None:
    """Maps a 5-min-of-day percentile band (compute_price_percentile_band())
    onto this solve's own real grid_times. Returns None (a complete no-op,
    matching GridConfig.import_price_upper/export_price_lower's own
    documented None-means-off default) if the band is empty (no history)
    -- a period with no matching bucket falls back to that period's own
    point price (network.py's own max(point, bound)/min(point, bound)
    logic then correctly treats this as "no adjustment," not a crash).
    """
    if not band_by_5min:
        return None
    out = []
    for i, gt in enumerate(grid_times):
        bucket = (
            solver_shared._local(gt).hour * 12 + solver_shared._local(gt).minute // 5
        )
        out.append(band_by_5min.get(bucket, point_price[i]))
    return out


def resample_price_with_extrapolation(
    forecast: list[dict],
    value_key: str,
    grid_times: list[datetime],
    aemo_pts: list[tuple[datetime, float]],
    offset_by_5min: dict[int, float],
) -> tuple[list[float], list[bool]]:
    """Same nearest-at-or-before lookup as resample_forecast() for any
    grid_time within the real forecast's own coverage -- but for periods
    BEYOND the last real data point, uses real AEMO forward spot data
    for that future period plus the real, 5-min-of-day retail offset
    (offset_by_5min, see compute_5min_offset()) instead of freezing the
    last real point flat. Falls back to the last real value if AEMO
    itself has no coverage for a given period (should not happen given
    AEMO's real 7.6-day coverage vs a 96h horizon, but defensive) --
    must never crash the writer.

    Also returns a per-period real-coverage mask alongside the values
    (2026-08-27, nimbus repo issue #216 -- same coverage-aware blending
    story as resample_generic_price_forecast_with_coverage(), see that
    function's own docstring). True only for a grid time within
    [this source's first real point, its last real point] -- False both
    before the first real point (the `pts[0][1]` placeholder below) and
    at/after the last one, since AEMO-extrapolated periods are a
    synthetic fill-in for THIS source, not a second genuine opinion.
    """
    pts = sorted(
        (
            (solver_shared.parse_iso(p["time"]), p[value_key])
            for p in forecast
            if p.get(value_key) is not None
        ),
        key=lambda x: x[0],
    )
    if not pts:
        return [0.0 for _ in grid_times], [False for _ in grid_times]
    first_real_time = pts[0][0]
    last_real_time = pts[-1][0]
    last_real_value = pts[-1][1]

    def nearest_before(
        source_pts: list[tuple[datetime, float]], gt: datetime
    ) -> float | None:
        if not source_pts:
            return None
        val = source_pts[0][1]
        for t, v in source_pts:
            if t <= gt:
                val = v
            else:
                break
        return val

    out = []
    real_mask = []
    for gt in grid_times:
        if gt <= last_real_time:
            val = pts[0][1]
            for t, v in pts:
                if t <= gt:
                    val = v
                else:
                    break
            out.append(float(val))
            real_mask.append(first_real_time <= gt)
            continue
        aemo_v = nearest_before(aemo_pts, gt)
        if aemo_v is not None:
            bucket = (
                solver_shared._local(gt).hour * 12
                + solver_shared._local(gt).minute // 5
            )
            out.append(float(aemo_v + offset_by_5min.get(bucket, 0.0)))
        else:
            out.append(float(last_real_value))
        real_mask.append(False)
    return out, real_mask
