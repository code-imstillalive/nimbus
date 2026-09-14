"""Solar forecast input gathering for the Solver cycle.

Moved verbatim out of `solver_writer.main()` (nimbus issue #735, stage 1)
-- a pure code-organization change, no behaviour change intended or made.
Every comment below is the original one; they carry real institutional
history (live household findings, reversed decisions, and the specific
regressions each guard exists to prevent) that would be lost by
paraphrasing them into a shorter module.

The three nested closures `main()` used to define (`fetch_solar_source_
safe`, `fetch_open_meteo_solar_raw`, `fetch_solcast_solar_raw`) are now
module-level privates taking `grid_times`/`n_periods` explicitly instead
of closing over `main()`'s own locals -- the only structural difference.

See this package's own `__init__.py` for why the `solver_writer` import
is deferred into each function body rather than done at module scope.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime

import numpy as np


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see this package's `__init__.py` for the full reasoning on
    both counts. Dual-mode try/except is the same shape every other
    project-internal import in `solver_writer` itself already uses: this
    code is loaded both as part of the real package and as a bare top-
    level module by the test harness (tests/_solver_path.py) and the
    standalone/cron deployment."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


def _fetch_solar_source_safe(
    entity_id: str,
    grid_times: list[datetime],
    n_periods: int,
) -> tuple[list[float], list[float], list[float]] | None:
    """(value, lower, upper) kW arrays for ONE solar source -- reads
    whichever shape _solar_entries_from_attributes() recognizes
    (generic forecast=[...], Solcast's own detailedForecast=[...],
    or Open-Meteo's own watts={...} -- nimbus issue #542), or None
    on any failure. See build_solar_arrays()'s own comment for why a
    missing source is DROPPED, never zero-filled.

    nimbus issue #543 (Mark Purcell): distinguishes an entity that's
    genuinely unreachable (HTTP/URL failure) from one that's healthy
    but publishes a shape none of the three readers above recognize
    (a real configuration fact, not a transient) -- the two used to
    share one generic "unavailable" message, which sent a household
    looking for a network/entity problem that didn't exist. Both
    reasons still drop the source from this cycle's blend either way.
    """
    sw = _solver_writer()
    try:
        attrs = sw.ha_get(entity_id)["attributes"]
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as e:
        sw._warn_solar_source_dropped_once(entity_id, "unavailable", str(e))
        return None
    fc = sw._solar_entries_from_attributes(attrs)
    if fc is None:
        sw._warn_solar_source_dropped_once(
            entity_id,
            "shape not recognized",
            "no forecast/detailedForecast/watts attribute",
        )
        return None
    try:
        # Real, honest clamp: a ML forecaster can produce a tiny
        # negative excursion near zero (physically impossible for
        # solar) -- found live on this script's very first real run.
        value = [max(0.0, v) for v in sw.resample_forecast(fc, "value", grid_times)]
        has_bounds = any(p.get("lower") is not None for p in fc)
        if has_bounds:
            lower = [max(0.0, v) for v in sw.resample_forecast(fc, "lower", grid_times)]
            upper = [max(0.0, v) for v in sw.resample_forecast(fc, "upper", grid_times)]
            lower = [min(lower[i], value[i]) for i in range(n_periods)]
            upper = [max(upper[i], value[i]) for i in range(n_periods)]
        else:
            lower = list(value)
            upper = list(value)
        sw._note_solar_source_recovered(entity_id)
        return value, lower, upper
    except (
        KeyError,
        # nimbus issue #363 (Mark Purcell): parse_iso() now normalizes
        # a naive timestamp to UTC rather than raising, but a source
        # publishing a genuinely unparseable (non-ISO) time string
        # still raises ValueError from datetime.fromisoformat() --
        # this except clause never caught that at all, so a bad
        # third-party source took down the ENTIRE solve cycle with a
        # traceback instead of just being dropped from the blend,
        # same as every other real failure mode here already is.
        TypeError,
        ValueError,
    ) as e:
        # nimbus issue #363: was print(..., file=sys.stderr) -- HA
        # does not route container stdout/stderr into its own log or
        # error_log, so this operationally-relevant warning (a real
        # solar source dropping out of the blend) was invisible to
        # anyone using the HA UI or `ha_get_logs`.
        sw._warn_solar_source_dropped_once(entity_id, "malformed", str(e))
        return None


def _fetch_open_meteo_solar_raw(
    grid_times: list[datetime],
) -> tuple[list[float], list[float], list[float]] | None:
    """Real, DIRECT read of Open-Meteo Solar Forecast's own 8 native
    entities (today/tomorrow/d2..d7) -- reshaped from their native
    watts={timestamp: value} dict shape via the same
    _solar_entries_from_attributes() every solar reader in this file
    now shares (nimbus issue #542 item 1), no intermediate HA
    template sensor. Auto-detected via entity_exists() on the
    anchor entity -- a complete no-op, not an error, on any install
    without Open-Meteo Solar Forecast. No real per-point uncertainty
    data exists from this source -- lower/upper mirror value (a
    zero-width band), same honest default as every other
    no-uncertainty source.

    nimbus issue #546: no skip_entities parameter -- dedup against a
    configured solver_solar_forecast_sensor_1/2/3 pointed at one of
    THESE SAME 8 entities happens at the caller, by skipping that
    configured source's own standalone fetch entirely rather than
    excluding one entity from THIS multi-entity read (see
    _is_known_solar_integration_entity()'s own docstring for the
    real regression an entity-level skip caused).
    """
    sw = _solver_writer()
    anchor = "sensor.home_energy_production_today"
    if not sw.entity_exists(anchor):
        return None
    entries: list[dict] = []
    for eid in sw._KNOWN_OPEN_METEO_SOLAR_ENTITY_IDS:
        if not sw.entity_exists(eid):
            continue
        fc = sw._solar_entries_from_attributes(sw.ha_get(eid)["attributes"])
        if fc:
            entries.extend(fc)
    if not entries:
        return None
    entries.sort(key=lambda e: e["time"])
    value = [max(0.0, v) for v in sw.resample_forecast(entries, "value", grid_times)]
    return value, list(value), list(value)


def _fetch_solcast_solar_raw(
    grid_times: list[datetime],
    n_periods: int,
) -> tuple[list[float], list[float], list[float]] | None:
    """Real, DIRECT read of Solcast's own 2 native entities
    (today/tomorrow) -- reshaped from their native detailedForecast
    list shape (30-min resolution, period_start/pv_estimate/
    pv_estimate10/pv_estimate90) via the same
    _solar_entries_from_attributes() every solar reader in this file
    now shares (nimbus issue #542 item 1), no intermediate HA
    template sensor. Auto-detected, a complete no-op on any install
    without Solcast. Carries Solcast's own REAL p10/p90 as genuine
    lower/upper confidence bounds -- a real bonus over Open-Meteo,
    which has none. pv_estimate is already kW average power for its
    30-min period, NOT the parent entity's own "kWh" unit tag
    (confirmed live, 2026-08-22: a real midday pv_estimate landed
    squarely between real measured solar and Open-Meteo's own kW
    value, not double that -- no unit conversion applied here.

    nimbus issue #546: no skip_entities parameter -- see
    _fetch_open_meteo_solar_raw()'s own docstring for why the dedup
    moved to the caller.
    """
    sw = _solver_writer()
    anchor = "sensor.solcast_pv_forecast_forecast_today"
    if not sw.entity_exists(anchor):
        return None
    entries: list[dict] = []
    for eid in sw._KNOWN_SOLCAST_SOLAR_ENTITY_IDS:
        if not sw.entity_exists(eid):
            continue
        fc = sw._solar_entries_from_attributes(sw.ha_get(eid)["attributes"])
        if fc:
            entries.extend(fc)
    if not entries:
        return None
    entries.sort(key=lambda e: e["time"])
    value = [max(0.0, v) for v in sw.resample_forecast(entries, "value", grid_times)]
    lower = [max(0.0, v) for v in sw.resample_forecast(entries, "lower", grid_times)]
    upper = [max(0.0, v) for v in sw.resample_forecast(entries, "upper", grid_times)]
    lower = [min(lower[i], value[i]) for i in range(n_periods)]
    upper = [max(upper[i], value[i]) for i in range(n_periods)]
    return value, lower, upper


def build_solar_arrays(
    cfg: dict,
    grid_times: list[datetime],
    n_periods: int,
) -> tuple[list[float], list[float], list[float]]:
    """Fetch, blend, and live-anchor every configured solar source into
    the three `(solar_kw, solar_lower_kw, solar_upper_kw)` arrays the LP
    element construction consumes.

    Solar forecast sources -- REAL raw sensors, read and blended
    directly in Python, no intermediate HA template sensor (2026-08-22,
    direct household ask: "why not input proper raw sensors they
    offer... i would much rather input proper raw sensors they offer
    and blend in solver" -- the earlier "combined" adapter template
    sensor approach genuinely worked but added an extra layer of
    indirection between the real integration and the solve; this
    reads each real integration's own native entities straight from
    the API and reshapes them here instead).

    Real household finding driving this, checked live: with only 2
    sources, BOTH were overpredicting real measured solar by 36-57%
    at the SAME moment [real 4.225kW vs source 1 6.639kW vs source 2
    5.73kW] -- proof two sources sharing the same directional bias
    don't cancel out when averaged, they just average the bias. A
    third, genuinely differently-modeled source (Solcast --
    satellite-imagery-anchored, architecturally distinct from both a
    self-trained ML model and a different NWP provider) gives the
    blend a real chance at partially-uncorrelated error.

    Source 1 (CONF_SOLVER_SOLAR_FORECAST_SENSOR) stays a generic,
    config-flow-pointed entity -- this is what keeps the Solver
    genuinely installable by anyone, not just this household (any
    source with a standard forecast:[{time,value,lower,upper}] shape
    works, including Nimbus's own self-trained model). Open-Meteo and
    Solcast are auto-detected directly by their own well-known real
    entity names (entity_exists() gated, a complete no-op on any
    install without them) -- no separate config field needed for
    these two specific, already-known integrations.

    EACH source is fetched independently and safely -- unlike the 18
    load forecasts (fetch_load_forecast_safe(), where "unavailable"
    safely defaults to 0.0 kW, a genuinely plausible real value for an
    idle circuit), a failed SOLAR source must NEVER be treated as 0kW:
    solar is essentially never legitimately zero during daylight hours,
    and silently blending in a false zero would drag the whole average
    WAY down -- actively corrupting an otherwise-healthy blend rather
    than degrading gracefully. On failure, that source is DROPPED from
    the average entirely; the blend runs across however many sources
    actually returned real data this cycle, never a phantom zero
    standing in for a missing one. (Direct household correction,
    2026-08-22: "make sure if one fails it returns 0 -- being wrapped?"
    -- the WRAPPING/never-crash instinct is right and applied here; the
    specific fallback VALUE had to be "drop", not "0", since 0kW solar
    mid-morning is never a safe assumption the way 0kW on one idle
    circuit out of 18 genuinely can be.)

    EQUAL weight across whichever sources succeed, deliberately --
    ml/blend.py's own weights_from_mae() already supports real
    accuracy-derived weighting, but there is no matured per-source
    accuracy data yet (Solver audit item #9's own capture-and-compare
    mechanism has only a handful of real snapshots so far). Averaging
    a known-bad source against a known-good one would make things
    WORSE -- but with genuinely UNKNOWN relative accuracy (the honest
    state right now), equal weight is the correct, defensible
    default, not a shortcut. Same, uniform treatment for EVERY
    period, including "now" -- no special-cased override anywhere.
    """
    sw = _solver_writer()

    solar_values, solar_lowers, solar_uppers = [], [], []

    # nimbus issue #546 (Mark Purcell, real regression the same day
    # #542/#543 shipped): whether auto-include is on, computed once up
    # front -- a configured source that resolves to a known Open-Meteo/
    # Solcast entity is skipped as a STANDALONE member whenever the
    # matching auto-include fetch is going to run anyway, so that
    # integration is represented exactly once, by its own full-coverage
    # multi-entity read, never by a second, narrower read of one of its
    # own entities. See _is_known_solar_integration_entity()'s own
    # docstring for the real regression the previous (entity-level
    # skip_entities) dedup caused.
    auto_include_known_solar = bool(cfg.get("solver_auto_include_known_solar"))

    def _skip_as_standalone_source(entity_id: str) -> bool:
        return auto_include_known_solar and sw._is_known_solar_integration_entity(
            entity_id
        )

    # Source 1: whatever's configured via the Solver settings wizard
    # (this household: Nimbus's own self-trained model).
    configured_entity = cfg.get("solver_solar_forecast_sensor")
    if configured_entity and not _skip_as_standalone_source(configured_entity):
        result = _fetch_solar_source_safe(configured_entity, grid_times, n_periods)
        if result is not None:
            v, lo, up = result
            solar_values.append(np.array(v))
            solar_lowers.append(np.array(lo))
            solar_uppers.append(np.array(up))

    # Known real integrations (Open-Meteo, Solcast) -- ONLY included if
    # switch.nimbus_solver_auto_include_known_solar is explicitly ON
    # (2026-08-22, direct household correction: this used to run
    # unconditionally, dressed up as "auto-detect", entirely outside the
    # 3 solar_forecast_sensor_1/2/3 config fields -- "then what is the
    # purposed of having 3 inputs since it forces user ot autodetect...
    # that feels wrong". Default is False (see Nimbus's own const.py
    # comment on CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR) -- a fresh
    # install gets exactly what's configured in sources 1/2/3, nothing
    # more, unless this switch is explicitly turned on.
    if auto_include_known_solar:
        for fetcher in (
            lambda: _fetch_open_meteo_solar_raw(grid_times),
            lambda: _fetch_solcast_solar_raw(grid_times, n_periods),
        ):
            result = fetcher()
            if result is not None:
                v, lo, up = result
                solar_values.append(np.array(v))
                solar_lowers.append(np.array(lo))
                solar_uppers.append(np.array(up))

    # Optional generic ADDITIONAL sources (solver_solar_forecast_
    # sensor_2/_3) -- for any OTHER solar forecast integration this
    # writer doesn't already know how to auto-detect (portability for
    # a different household's own install), or a second pointer at the
    # same known integration if wanted. Blank (the default) contributes
    # nothing, same guarantee as every other optional field.
    for entity_id in (
        cfg.get("solver_solar_forecast_sensor_2"),
        cfg.get("solver_solar_forecast_sensor_3"),
    ):
        if not entity_id or _skip_as_standalone_source(entity_id):
            continue
        result = _fetch_solar_source_safe(entity_id, grid_times, n_periods)
        if result is not None:
            v, lo, up = result
            solar_values.append(np.array(v))
            solar_lowers.append(np.array(lo))
            solar_uppers.append(np.array(up))

    if not solar_values:
        # Real bug found live (nimbus repo issue #115, Mark Purcell, a
        # real independent installer's own live health-check,
        # 2026-08-24): this used to `raise RuntimeError`, refusing to
        # solve AT ALL, ~470 times over an 8-hour overnight window on a
        # real install -- every single one of his configured solar
        # sources genuinely producing no data during the exact hours
        # solar is expected to be zero anyway (sunset to sunrise). This
        # is the WRONG failure mode for a condition that recurs every
        # single night on every solar install: the solver going
        # completely blind for hours (no re-optimisation against
        # changing overnight prices, no recovery from an unrelated
        # entity going unavailable until the next daylight cycle) is a
        # much worse outcome than solving with a real, honest 0.0 kW
        # solar placeholder -- exactly matching the flat-0.0-on-failure
        # convention already established for load
        # (read_load_forecast_sensor()'s own error path) and every
        # other genuinely-optional input in this file. A loud WARNING
        # (not a silent fallback) still fires so this is visible in the
        # log, same as the load-forecast equivalent.
        sw._LOGGER.warning(
            "Nimbus Solver: no solar forecast source produced any real "
            "data this cycle (all configured sources unavailable, or none "
            "configured) -- solving with a flat 0.0 kW solar placeholder "
            "instead of refusing to solve. This is expected and harmless "
            "overnight (0.0 kW solar overnight is the correct real value "
            "regardless); if this fires during genuine daylight hours, "
            "check that at least one solver_solar_forecast_sensor_*/"
            "auto-include-known-solar source is configured and reachable."
        )
        solar_values = [np.zeros(n_periods)]
        solar_lowers = [np.zeros(n_periods)]
        solar_uppers = [np.zeros(n_periods)]

    if len(solar_values) == 1:
        solar_kw = [float(v) for v in solar_values[0]]
        # elements.py's own _validate_confidence_band() requires
        # lower_kw <= forecast_kw <= upper_kw exactly -- see the same
        # defensive clamp already applied per-source above.
        solar_lower_kw = [float(v) for v in solar_lowers[0]]
        solar_upper_kw = [float(v) for v in solar_uppers[0]]
    else:
        blended = sw.blend_forecast_array(solar_values)
        # cross_source_spread() widens the confidence band by the real
        # DISAGREEMENT between the sources that actually succeeded this
        # cycle -- sources that agree closely add little; sources that
        # disagree sharply (exactly what was found live) genuinely
        # should make the Solver less confident at that specific
        # period, feeding the already-proven risk_aversion mechanism a
        # real, earned signal instead of just one source's own
        # (possibly overconfident) band.
        spread = sw.cross_source_spread(solar_values)
        solar_kw = [max(0.0, v) for v in blended]
        combined_lower = np.min(np.stack(solar_lowers, axis=0), axis=0)
        combined_upper = np.max(np.stack(solar_uppers, axis=0), axis=0)
        solar_lower_kw = [
            max(0.0, min(combined_lower[i], solar_kw[i]) - spread[i] / 2)
            for i in range(n_periods)
        ]
        solar_upper_kw = [
            max(combined_upper[i], solar_kw[i]) + spread[i] / 2
            for i in range(n_periods)
        ]

    # Real, live anchor for the CURRENT period ONLY (2026-08-22, direct
    # household decision, after Mark Purcell's own question: "Why
    # doesn't NIMBUS use actuals for the current solar interval?").
    # Earlier the SAME day, an equivalent mechanism was explicitly
    # declined ("i do not want tricks") when it looked like it would
    # just make the forecast LOOK more accurate without the underlying
    # model improving. Reconsidered and reversed, deliberately, on a
    # different justification: the current interval is the one period
    # where the real answer is already known via direct measurement,
    # not something that needs predicting at all -- using it isn't a
    # trick, it's just not discarding information already in hand. HAEO
    # does exactly this already (confirmed earlier the same day: this
    # is why its own solar figure looks "spot on" for right now, not
    # because its underlying forecasting is better).
    #
    # CONF_SOLVER_SOLAR_POWER_SENSOR (was hardcoded to this household's
    # own sensor.combined_total_dc_power until the 2026-09-02 audit --
    # the SAME real, physical Sungrow DC-power measurement (W) already
    # wizard-configured for compute_daily_quality_report()'s own oracle
    # scoring; confirmed live 2026-09-02 to already hold the identical
    # entity, so this is a pure hardcode-removal, zero behaviour change
    # for this household). Confirmed live, same day this cross-check was
    # first built: 3.80kW measured vs 5.57kW forecast at the same
    # instant, a real, meaningful divergence this closes. Deliberately
    # scoped to index 0 ONLY -- every other period stays a genuine
    # forecast, nothing propagates beyond "right now". Zero-width
    # confidence band at this point -- a known, measured value has no
    # forecast uncertainty to represent. Any read failure (blank field,
    # missing/unavailable entity, bad value) leaves solar_kw[0] as the
    # forecast value, same graceful-degradation convention as every
    # other optional source in this file.
    solar_power_sensor = cfg.get("solver_solar_power_sensor")
    if solar_power_sensor and sw.entity_exists(solar_power_sensor):
        try:
            # nimbus issue #453 (Mark Purcell): this used to divide by
            # 1000.0 unconditionally, the same undocumented "must be
            # Watts" assumption _kw_scale_factor()'s own docstring
            # already documents as a real, confirmed-live bug elsewhere
            # in this file (compute_daily_quality_report()/compute_
            # efficiency_backtest_report(), 2026-08-28) -- correct for
            # THIS household (sensor.combined_total_dc_power's own
            # unit_of_measurement is genuinely "W", confirmed live), but
            # silently corrupts solar_kw[0] toward ~0 for any other
            # install whose configured sensor already reports kW. Reuses
            # the same helper instead of a second hardcoded assumption
            # of the identical shape -- zero behaviour change for this
            # household (0.001 either way), real fix for a kW-native one.
            live_solar_kw = float(
                sw.ha_get(solar_power_sensor)["state"]
            ) * sw._kw_scale_factor(solar_power_sensor)
            solar_kw[0] = max(0.0, live_solar_kw)
            solar_lower_kw[0] = solar_kw[0]
            solar_upper_kw[0] = solar_kw[0]
        except (ValueError, KeyError, TypeError):
            pass

    return solar_kw, solar_lower_kw, solar_upper_kw
