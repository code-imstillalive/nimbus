"""The HA-I/O and config-resolution core shared by every reporting
function `solver_writer.py`'s own god-module still carries.

nimbus issue #1301 (Phase 2), prerequisite step "2a" -- spec 001
(`docs/specs/001-solver-shared-ha-io-core.md`, #1347). Phase 2 needs to
move eight reporting functions out of `solver_writer.py` into
`solver_reports/{quality,backtest,counterfactual,flex}.py`. Measured by
`tests/analyse_module_dependencies.py --phase 2`: all eight depend on a
shared set of module-level names still living in `solver_writer.py` --
none of which are reporting logic. They are recorder access (`ha_get`,
`ha_post_state`, `fetch_entity_history_range`,
`fetch_entity_attribute_history_range`, `resample_history_nearest`,
`resample_history_mean`), config resolution (`_cfg_num`, `_cfg_int`,
`resolve_effective_capacity_kwh`, `import_fee_rate`,
`fetch_p2p_fixed_export_kw`, `p2p_bonus_price_by_period`), and small
utilities (`_LOGGER`, `LOCAL_TZ`, `_local`, `_kw_scale_factor`,
`_version_stamp`, `TIER1_PERIOD_HOURS`, `TIER2_PERIOD_HOURS`,
`MAX_TIER1_HOURS`). This module gives them a home below
`solver_reports/` in the tech-debt plan's own layer map, so Phase 2's
own extraction has something to import from instead of reaching back
into `solver_writer.py`.

This module does NOT move the eight reporting functions themselves --
that is Phase 2 proper (2b-2e), against this module once it exists.

**Bodies moved verbatim, signatures unchanged** -- this is a pure
relocation, not a redesign. Every name here keeps a re-export in
`solver_writer.py` (the façade decision spec 001 records at length: 48
`ha_get` monkeypatch sites and 29+ `ha_post_state` ones make retargeting
every call site in the same PR a large, error-prone diff for zero
behavioural gain).

**`_LOGGER` is a genuine alias, not a separately-created logger.** This
module creates the one real `_LOGGER = logging.getLogger(__name__)`
(`nimbus_load.solver_shared`), and `solver_writer.py` imports it rather
than creating its own -- `solver_writer._LOGGER is solver_shared._LOGGER`,
confirmed by a real identity test, not two independent loggers under
different names. An earlier draft of spec 001 had this backwards
(`solver_writer.py` keeping its own independently-created `_LOGGER`, on
the theory that some test asserts on a logger's own name); peer review
(PR #1347) actually ran that check rather than assuming it and found the
opposite: zero tests assert a logger by name string, while four existing
suites (`test_solver_inputs_solar.py`,
`test_solver_writer_battery_participants.py`,
`test_solar_source_shape_and_dedup.py`,
`test_quality_report_achieved_energy_and_reliability.py`) capture log
lines via `assertLogs(solver_writer._LOGGER, ...)` on modules that log
through the six `solver_inputs/*.py` files this same spec repoints onto
`solver_shared`'s own logger. Since `assertLogs` on a Logger *object*
only captures records logged through that exact object (Python's
logging hierarchy is name-based, and `nimbus_load.solver_writer`/
`nimbus_load.solver_shared` are siblings, not ancestor/descendant),
keeping them as two separate loggers would have broken those four
suites the moment the six modules repoint -- aliasing is what keeps
them passing.

**The alias does NOT make `patch.object(solver_writer, "_LOGGER", ...)`
equivalent to `patch.object(solver_shared, "_LOGGER", ...)`, though.**
`patch.object` replaces a NAMESPACE BINDING on whichever module you name,
not the underlying object everywhere it's referenced -- a function
defined here (e.g. `resolve_effective_capacity_kwh`) resolves `_LOGGER`
from THIS module's own globals at call time, so a test that needs to
intercept its logging has to patch `solver_shared._LOGGER` specifically
(nimbus issue #861's exact failure mode, same as any other name in this
file). `assertLogs` has no such gap because it takes a live object
reference rather than a namespace+name pair.

**The injected `hass` lives HERE, on `NATIVE`, not on `solver_writer`**
(nimbus issue #1437). It used to be `solver_writer._NATIVE_HASS`, a module
name REBOUND by `set_native_hass()` -- spec 001's own named exception, since
a rebound name cannot be imported (an import freezes it at `None`), so every
reader went through the deferred `_solver_writer()` seam. `NATIVE` is a
holder whose identity never changes and whose `hass` attribute is mutated,
so any module can import it at module scope and read `NATIVE.hass` at call
time. `solver_writer` re-exports it, so `solver_writer.NATIVE is NATIVE`.

**`HA_BASE`/`_load_token()` still stay behind** in `solver_writer.py`,
reached here via the deferred, by-MODULE `_solver_writer()` seam
`solver_inputs/*.py`/`solver_publish.py` also use (see either module's own
`_solver_writer()` docstring for the "relocating a function also relocates
who its internal callers resolve" story, nimbus issue #861):
`tests/test_solver_writer_import_and_token_laziness.py` source-scans
`solver_writer.py` itself for the literal `open(TOKEN_PATH` call site inside
a function literally named `_load_token()` -- moving that body would break a
real, existing, unedited test.

**Two private companions moved alongside their one real caller**, neither
literally named in spec 001's own 19-name interface list because neither
is itself a Phase-2 blocker -- but both are genuine, load-bearing
dependencies of a name that IS, confirmed via
`tests/analyse_module_dependencies.py --functions <name>` before moving
anything: `_native_http_error` (ha_get's own native-mode 404 shape) and
`_warn_if_attrs_exceed_recorder_cap` (+ its own `_MAX_STATE_ATTRS_BYTES`/
`_OVERSIZE_ATTRS_WARNED` state, `ha_post_state`'s own #944 recorder-cap
diagnostic). Also moved for the identical reason: `NETWORK_FEE_BLOCK_KEYS`
(`import_fee_rate`'s own private table), `P2P_BLOCK_KEYS` (`fetch_p2p_
fixed_export_kw`'s own), `_SOH_RANGE_WARNED` (`resolve_effective_capacity_
kwh`'s own dedup state -- still reached as `solver_writer._SOH_RANGE_
WARNED` by an existing test, which the re-export keeps working since it's
the identical mutable `set` object either way), `_nimbus_version` (`_version_
stamp`'s own `@functools.cache`d manifest reader -- also has a second,
staying caller inside `_carry_forward_quality_history`, which keeps
resolving it correctly via the re-exported binding in `solver_writer`'s own
namespace), `parse_iso` (a genuinely shared helper with many staying
callers too, moved wholesale since it is a pure function with no test
depending on its literal definition site), and `_ENTITY_UPDATE_HANDLERS`/
`_NATIVE_MANAGED_ENTITY_IDS` (`ha_post_state`'s own native dispatch-table
state -- also read by `register_entity_handler()`/`unregister_entity_
handler()`, which stay in `solver_writer.py` and keep working against the
identical re-exported dict/frozenset objects).
"""

from __future__ import annotations

import functools
import io
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

try:
    from .power_units import power_scale_to_kw
except ImportError:  # pragma: no cover - standalone/cron path
    from power_units import power_scale_to_kw  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see `solver_publish.py`'s own `_solver_writer()` for the full
    reasoning, and nimbus issue #861 for the concrete failure that makes
    it load-bearing: relocating a function also relocates who its
    internal callers resolve, escaping every `patch.object(solver_writer,
    ...)` in the suite and turning a mocked call into a live HTTP
    request.

    Used here only for the names spec 001 documents as staying behind in
    `solver_writer.py` -- `HA_BASE` and `_load_token()` -- never for a name
    this module itself owns. (`_NATIVE_HASS` was the third until #1437
    replaced it with `NATIVE` below.)
    """
    try:
        from . import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


class _NativeContext:
    """Where the in-process `hass` lives (nimbus issue #1437).

    `hass` is None in the standalone/cron/REST deployment and a real
    `HomeAssistant` once `solver_writer.set_native_hass()` has run inside the
    integration. The point is the STABLE IDENTITY: `NATIVE` is created once
    and never rebound, only its `hass` attribute is mutated -- so
    `from .solver_shared import NATIVE` is safe at module scope, where
    importing the old rebound `_NATIVE_HASS` name froze it at `None`.

    `__slots__` is deliberate: a misspelt write (`NATIVE.has = stub`) raises
    AttributeError instead of silently creating an attribute nothing reads --
    the silent-no-op class #1434 is about, closed for this object by
    construction.

    Tests set it with `patch.object(solver_writer.NATIVE, "hass", stub)` or
    plain assignment; both write the one object every reader reads.
    """

    __slots__ = ("hass",)

    def __init__(self) -> None:
        self.hass = None


NATIVE = _NativeContext()


# ---------------------------------------------------------------------------
# Local-timezone conversion + the tiered-horizon period constants.
# ---------------------------------------------------------------------------

LOCAL_TZ = ZoneInfo(os.environ.get("NIMBUS_SOLVER_TIMEZONE", "Australia/Brisbane"))


def _local(ts):
    """The same instant, expressed in the household's OWN timezone.

    Every hour-of-day decision in this file -- the P2P window, network
    fee tiers, scheduled discharge cost, salvage value, the self-consume
    block, EV departure -- is a statement about LOCAL wall-clock time. A
    household's 17:00 P2P block is 17:00 where they live, not 17:00 UTC.

    Reading `.hour` straight off a datetime silently makes that decision
    depend on whatever timezone the CALLER attached, which is not a
    property this file controls: the same functions are reached from the
    native integration, from the cron writer, and from a service call
    whose `start` a user types. On the daily path `grid_times` carry
    UTC -- visible in the report's own j_ref_hourly keys, which are
    stamped +00:00 -- so a bare `gt.hour` gated the reference
    household's real 17:00-24:00 P2P block against 17:00-24:00 UTC,
    i.e. 03:00-10:00 Brisbane. That is their solar CHARGING window,
    where grid export is genuinely ~0, so the scorer recorded a 12 kW x
    7 h commitment as entirely undelivered: p2p_commitment_shortfall_kwh
    76.9 against a real value near zero, which then lands straight on
    j_ach and depresses EPR.

    This file's own line 249 records the same fault being found and
    fixed at ONE site; the pattern that allowed it was left everywhere
    else.

    So: convert explicitly, never trust the input. On a datetime already
    in LOCAL_TZ this is an exact no-op.

    A NAIVE datetime is returned unchanged rather than guessed at.
    `.astimezone()` on a naive value silently assumes the machine's own
    timezone -- a second, different wrong answer. Unchanged is exactly
    the previous behaviour, so this can never make a naive caller worse.
    """
    return ts.astimezone(LOCAL_TZ) if getattr(ts, "tzinfo", None) is not None else ts


# Tiered horizon (2026-08-16, real ask: "how about 5 days forecast?" /
# "how about 96hrs?"), same real architecture HAEO's own horizon already
# uses ("minute-resolution tiers for the first 5 minutes, then 5-min
# tiers, then hourly" -- this project's own documented HAEO design).
#
# nimbus issue #451 (Mark Purcell): TIER1_HOURS used to be a fixed 24.0 --
# tier1's own 5-min resolution ran for a full real day regardless of what
# the upstream price data could actually support at that resolution.
# Real finding: Nimbus does not have a genuine 5-minute-resolution
# forward price for 24h. LocalVolts costsFlexUp/earningsFlexUp is a
# genuine 5-min number, but only for the CURRENT settlement interval;
# AEMO's own PD7DAY predispatch report (the longer-horizon price source)
# is natively 30-min. Every "5-min" period beyond the current+next real
# NEM trading interval was really just one of those 30-min numbers
# stamped six times with compute_5min_offset()'s own historical bump on
# top -- fake precision, paid for in real solver load (365 periods at
# the old shape vs ~200 now). Tier1 is now boundary-snapped to the real
# current + next NEM trading interval (:00/:30) instead of a fixed
# duration -- see build_tiered_grid()'s own docstring for the exact
# mechanism.
TIER1_PERIOD_HOURS = 5.0 / 60.0
TIER2_PERIOD_HOURS = 0.5  # was 1.0 -- matches AEMO PD7DAY's own real 30-min cadence

# The real, ceiling-not-typical max span tier1 can ever be now that it's
# boundary-snapped rather than fixed-duration -- "current + next 30-min
# trading interval" tops out at 60 real minutes (tier1_start landing
# exactly on a :00/:30 mark), never more. Used by the two report-scoring
# functions (compute_daily_quality_report/compute_efficiency_backtest_
# report) to decide whether a scored window fits entirely inside tier1's
# own real span -- see nimbus issues #438/#441 for why this matching
# matters (a report scored at the wrong resolution silently disagrees
# with what the live dispatch it's grading actually did).
MAX_TIER1_HOURS = 1.0


# ---------------------------------------------------------------------------
# Config resolution.
# ---------------------------------------------------------------------------


def _cfg_num(cfg: dict, key: str, default: float) -> float:
    """Return ``float(cfg[key])`` unless the value is missing/None, in
    which case return ``default``.

    REPLACES the pervasive ``float(cfg.get(key) or default)`` pattern
    that used to appear throughout this file. That pattern silently
    treats an intentional ``0.0`` as "unset" (0.0 is falsy in Python)
    and swaps in the hardcoded default -- a real, live footgun for any
    field where 0 is a legitimate user setting (min_soc_percent set to
    0% by an installer as a temporary bypass being the specific case
    that surfaced this: setting the dashboard number entity to 0.0 was
    byte-identical to leaving it unset, and the solver silently reverted
    to the 5.0% default and kept crashing).

    Using ``is None`` distinguishes "user set 0" from "never configured,"
    which is what a genuine numeric default should do. For fields where
    the hardcoded default IS ``0.0``, this helper is functionally
    identical to the old pattern -- swapped anyway for consistency and
    defensiveness against a future default change.
    """
    val = cfg.get(key)
    if val is None:
        return default
    return float(val)


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    """Same as ``_cfg_num`` but returns ``int``. Used for hour-of-day
    block boundaries where ``0`` is a legitimate value (midnight).
    """
    val = cfg.get(key)
    if val is None:
        return default
    return int(val)


_SOH_RANGE_WARNED: set[float] = set()


def resolve_effective_capacity_kwh(cfg: dict) -> float:
    """Nameplate battery capacity derated by configured State of Health.

    nimbus issue #1013. `number.nimbus_solver_battery_soh_percent` has
    existed as a dashboard dial, been mirrored onto
    `sensor.nimbus_solver_config`, and been read by **nothing** -- no
    solve path, native or cron. A household setting State of Health
    changed nothing about dispatch or scoring, silently. The reference
    household had it at 98%, in the reasonable belief it derated a 122.2
    kWh pack to ~119.8, while every solve planned against the full
    122.2.

    The semantics implemented here are not invented: const.py's own
    comment beside CONF_SOLVER_BATTERY_SOH_PERCENT has documented
    `effective_capacity = capacity_kwh * soh_percent / 100` since the
    field was added. Only the code was missing.

    **Why this derates both rails rather than only the ceiling** -- the
    one real modelling choice here, and it was measured rather than
    argued. Fourteen consecutive days of the reference household's own
    daily statistics, two sensors describing one pack:

        sensor.combined_battery_charge   max 119.72 kWh  min 2.39 kWh
        sensor.logger_battery_level_soc  max 100.0 %     min 2.0 %
        sensor.combined_battery_capacity     122.16 kWh, constant

    The BMS reports 100% at **119.72 kWh, not at the 122.16 nameplate**
    -- so SoC is a percentage of the pack's CURRENT usable capacity, and
    the nameplate is not the scale the household's own SoC sensor speaks
    in. The floor agrees independently: Min SoC is configured at 2.0%
    and the daily minimum lands at 2.39 kWh, which is 2.0% of 119.72
    (2.394) rather than of 122.16 (2.443).

    So both bounds must come off the derated number, or the solver's kWh
    and the SoC sensor are describing different batteries. Derating only
    the ceiling would silently reserve MORE real energy at the floor
    than the household asked for.

    The configured value is also vindicated by the same reading: 122.2 x
    0.98 = 119.756 against a measured 119.72, a difference of 0.03%. The
    dial was right the whole time. Nothing read it. See
    tests/test_effective_capacity_soh_derating.py for the full data.

    **Deliberately still one static number, not a live sensor.** The
    inverters do publish per-pack SoH and it drifts over years, and
    CONF_BATTERY_TOWER_SOH_SENSOR already exists in the topology
    subentry -- but const.py's own comment is explicit that this field
    is "one number the owner updates occasionally... NOT an automated
    fade-tracking model." Wiring the live sensor is a real follow-up,
    not something to smuggle in under a bug fix.

    No-op by default: DEFAULT_SOLVER_SOH_PERCENT is 100.0, so any
    install that has never touched the dial gets a byte-identical
    number back.

    A reading outside (0, 100] returns nameplate UNCHANGED rather than
    scaling by it. number.py clamps the entity to [1, 100], so this is
    only reachable from hand-edited config or the cron copy's own YAML
    -- and a nonsense value must not silently shrink a real pack, nor
    inflate one beyond its nameplate.
    """
    nominal = _cfg_num(cfg, "solver_battery_capacity_kwh", 0.0)
    soh_pct = _cfg_num(cfg, "solver_battery_soh_percent", 100.0)
    if not 0.0 < soh_pct <= 100.0:
        if soh_pct not in _SOH_RANGE_WARNED:
            _SOH_RANGE_WARNED.add(soh_pct)
            _LOGGER.warning(
                "Nimbus Solver: configured Battery State of Health is "
                "%.2f%%, outside the valid (0, 100] range -- ignoring it "
                "and planning against the full %.2f kWh nameplate "
                "capacity for this solve. Set it between 1 and 100 on the "
                "dashboard if you meant to derate an aged pack.",
                soh_pct,
                nominal,
            )
        return nominal
    return nominal * soh_pct / 100.0


# Same real, live config-flow keys as P2P_BLOCK_KEYS below, mirrored
# exactly for network TOU fees (2026-08-22 -- see import_fee_rate()'s
# own docstring for the full "no hardcoded tariff" story).
NETWORK_FEE_BLOCK_KEYS = (
    (
        "solver_network_fee_1_rate",
        "solver_network_fee_1_start_hour",
        "solver_network_fee_1_end_hour",
    ),
    (
        "solver_network_fee_2_rate",
        "solver_network_fee_2_start_hour",
        "solver_network_fee_2_end_hour",
    ),
    (
        "solver_network_fee_3_rate",
        "solver_network_fee_3_start_hour",
        "solver_network_fee_3_end_hour",
    ),
)


def import_fee_rate(cfg: dict, hour: int) -> float:
    """Real, live, dashboard-configurable network TOU fee for a given
    hour -- REPLACES the old hardcoded network_energy_rate()
    (2026-08-22, direct household demand after the Buy¢/Fees¢ split:
    "how do they configure fees column... I TOLD U NO HARDCODED INPUTS
    - this has to work as user setting"). Every household's own real
    tariff is completely different (retailer, network, region, TOU
    structure) -- there is no universal default, so this reads live
    number.nimbus_solver_network_fee_* entities instead of a Python
    constant.

    Same shape as fetch_p2p_fixed_export_kw()'s own P2P blocks: a
    DEFAULT rate (the baseline/"shoulder" rate applied to any hour not
    covered by an override block) plus up to 3 optional override
    blocks (rate<=0 = "not configured", same convention as the P2P
    blocks). A flat single-rate tariff sets only the default and
    leaves all 3 blocks off; a 2-tier peak/offpeak retailer sets
    default=offpeak + one block=peak; a 3-tier retailer (this
    household's own real Energex NTC 6900 structure) sets
    default=shoulder + block1=peak + block2=offpeak. Leaving
    everything at its 0.0 default (a fresh install, or anyone who
    hasn't configured this) correctly makes this a complete no-op --
    Fees¢ shows 0, same honest "no data assumed" default as every
    other optional Solver field.
    """
    default_rate = _cfg_num(cfg, "solver_network_fee_default_rate", 0.0)
    for rate_key, start_key, end_key in NETWORK_FEE_BLOCK_KEYS:
        rate = _cfg_num(cfg, rate_key, 0.0)
        start_hour = _cfg_int(cfg, start_key, 0)
        end_hour = _cfg_int(cfg, end_key, 0)
        if rate <= 0 or end_hour <= start_hour:
            continue
        if start_hour <= hour < end_hour:
            return rate
    return default_rate


# ---------------------------------------------------------------------------
# Recorder / real-time HA I/O.
# ---------------------------------------------------------------------------


def parse_iso(s) -> datetime:
    # Real bug, confirmed live 2026-08-22 (first-ever native-mode run):
    # every call site here was written and only ever tested against
    # REST-sourced data, where a timestamp is ALWAYS a plain string --
    # HA's own JSON serialization stringifies every datetime on the way
    # out over HTTP, so REST mode's ha_get() never sees anything else.
    # Native mode's ha_get() (solver_runtime.py / set_native_hass())
    # reads state.attributes directly, the RAW unserialized Python
    # object -- and at least one real integration (Solcast, confirmed
    # via the live traceback) stores its own forecast[]'s own "time"
    # field as a genuine datetime object internally, not a string.
    # `s.replace("Z", "+00:00")` on a real datetime silently resolves to
    # datetime.replace() instead of str.replace() -- a completely
    # different method (year/month/day/... as integers), so Python
    # raises the confusing "'str' object cannot be interpreted as an
    # integer" rather than any hint this was ever a type mismatch.
    if isinstance(s, datetime):
        # Already a real datetime -- nothing to parse. HA's own internal
        # convention is that stored datetimes are timezone-aware
        # (almost certainly true here), but fall back to explicit UTC on
        # the off chance it's naive, matching what a bare "...Z"-suffixed
        # string would have meant on the REST-mode path above.
        return s if s.tzinfo is not None else s.replace(tzinfo=UTC)
    # nimbus issue #363 (Mark Purcell): a third-party source can genuinely
    # publish an offset-less ISO string (e.g. "2026-09-04T12:00:00", no
    # "Z"/"+00:00" suffix) -- datetime.fromisoformat() on that produces a
    # NAIVE datetime, which every real caller of this function (sorting
    # against other tz-aware timestamps in resample_forecast(),
    # .astimezone() calls, PeriodGrid arithmetic) assumes never happens.
    # Comparing a naive result against grid_times raises TypeError deep
    # inside resample_forecast() -- and fetch_solar_source_safe()'s own
    # except clause only catches HTTPError/URLError/KeyError/
    # JSONDecodeError, so this took down the ENTIRE solve cycle with a
    # traceback instead of the one source being safely dropped. Same
    # "assume UTC for a genuinely naive value" treatment the datetime
    # branch above already gets.
    parsed = datetime.fromisoformat(s)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _native_http_error(entity_id: str, code: int, msg: str) -> urllib.error.HTTPError:
    # A real, well-formed HTTPError -- not a synthetic ad-hoc exception --
    # specifically so every one of this file's own existing
    # `except urllib.error.HTTPError as e: if e.code == 404` sites
    # (entity_exists, fetch_load_forecast_safe, fetch_solar_source_safe,
    # and several more scattered through main()) keeps working completely
    # unchanged in native mode too. fp=a real BytesIO (not None) matters:
    # the top-level __main__ guard's own `except ... e.read()` would
    # crash on a bare HTTPError(fp=None) if this were ever reached that
    # way instead -- confirmed by reading urllib.error.HTTPError's own
    # real implementation (it's a thin wrapper over its own `fp`).
    return urllib.error.HTTPError(
        url=f"native://{entity_id}",
        code=code,
        msg=msg,
        hdrs=None,
        fp=io.BytesIO(msg.encode("utf-8")),
    )


def ha_get(entity_id: str) -> dict:
    sw = _solver_writer()
    if NATIVE.hass is not None:
        # hass.states.get() is a plain, synchronous, in-memory dict
        # lookup (HA's own state machine) -- real, established practice
        # to call it from a worker thread (see solver_runtime.py's own
        # module docstring for the full "why this is safe" reasoning),
        # not textbook-perfect event-loop-only HA threading but the
        # pragmatic, low-risk choice given the alternative is restructuring
        # ~2400 lines of already-correct, already-live-tested logic.
        state = NATIVE.hass.states.get(entity_id)
        if state is None:
            raise _native_http_error(entity_id, 404, f"Entity {entity_id} not found")
        out = {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
        }
        # nimbus #1537: the REST path already returns last_updated; the
        # P2P matched-rate merge compares two sources' freshness by it.
        updated = getattr(state, "last_updated", None)
        if isinstance(updated, datetime):
            out["last_updated"] = updated.isoformat()
        return out
    req = urllib.request.Request(
        f"{sw.HA_BASE}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {sw._load_token()}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# nimbus issue #944: the recorder's own per-state attribute cap
# (homeassistant/components/recorder/db_schema.py::MAX_STATE_ATTRS_BYTES).
# Mirrored here rather than imported because this module must keep
# working on the standalone/cron deployment, which has no homeassistant
# package at all.
_MAX_STATE_ATTRS_BYTES = 16384

# Warn once per entity_id per process. The condition is structural -- if
# a payload is over the cap this cycle it will be over it every cycle --
# so repeating it once a minute would be exactly the noise v0.94.297 had
# to clean up for #757.
_OVERSIZE_ATTRS_WARNED: set[str] = set()


def _warn_if_attrs_exceed_recorder_cap(entity_id: str, attributes: dict) -> None:
    """Say plainly when a published payload will lose ALL of its
    attribute history (nimbus issue #944).

    `_unrecorded_attributes` is what normally keeps the big per-period
    series out of the recorder, and it CANNOT apply here. HA reads it
    from `state.state_info`, which only the entity path populates;
    `states.async_set()` and the REST API both leave it `None`. So on
    these two publish paths the whole payload is measured, and once it
    exceeds the cap the recorder drops **every attribute on the row**
    (`return b"{}"`), not merely the oversized one.

    The knock-on is what makes this worth a warning rather than a
    comment: `unit_of_measurement` goes with the rest, the statistics
    compiler then sees no unit where it previously compiled one, and
    long-term statistics for that entity are suppressed. The only thing
    in the log today is a recorder line that reads like a database
    performance note and names neither Nimbus nor the consequence.

    Best-effort throughout: a diagnostic must never be the reason a real
    publish fails.
    """
    try:
        if entity_id in _OVERSIZE_ATTRS_WARNED or not attributes:
            return
        encoded = json.dumps(attributes).encode("utf-8")
        if len(encoded) <= _MAX_STATE_ATTRS_BYTES:
            return
        _OVERSIZE_ATTRS_WARNED.add(entity_id)
        biggest = sorted(
            ((len(json.dumps(v).encode("utf-8")), k) for k, v in attributes.items()),
            reverse=True,
        )[:3]
        _LOGGER.warning(
            "Nimbus #944: %s is publishing %d bytes of attributes, over the "
            "recorder's %d byte cap, on a publish path that cannot apply "
            "_unrecorded_attributes (no state_info: REST, or the raw "
            "states.async_set fallback). The recorder will therefore drop "
            "ALL attributes for this entity -- including unit_of_measurement, "
            "which then suppresses its long-term statistics. Largest "
            "contributors: %s. The live state is unaffected; what is lost is "
            "history. Logged once per entity per run.",
            entity_id,
            len(encoded),
            _MAX_STATE_ATTRS_BYTES,
            ", ".join(f"{name}={size}B" for size, name in biggest),
        )
    except Exception:
        _LOGGER.debug(
            "Nimbus #944: could not measure the attribute payload for %s "
            "(unserialisable value). Skipping the size check for this "
            "publish -- the publish itself is unaffected.",
            entity_id,
            exc_info=True,
        )


# PURE INTEGRATION dispatch seam (2026-08-23, issue #55) -- lets
# sensor.py's real SensorEntity classes register themselves as the
# native-mode handler for a specific entity_id, so ha_post_state() below
# routes state updates for that entity_id through the entity's own
# async_write_ha_state() path (proper unique_id, device_info, device_
# class, unrecorded_attributes, etc.) instead of writing a raw dict
# straight into the state machine via states.async_set(). Empty by
# default -- an unregistered entity_id still falls through to the
# original states.async_set() fallback (preserving behaviour for anyone
# still on a version that hasn't migrated its SensorEntities yet), and
# the REST path below is completely unaffected.
#
# Also read/written by register_entity_handler()/unregister_entity_
# handler(), which stay in solver_writer.py -- same object either side of
# the re-export, since this is a plain mutable dict, mutated in place
# (never reassigned).
_ENTITY_UPDATE_HANDLERS: dict[str, object] = {}

# Every entity_id sensor.py's own async_setup_entry() DOES eventually call
# register_entity_handler() for, in native mode (2026-09-01, real root
# cause of issue #312's residual -- see __init__.py's own async_unload_
# entry() comment for the full incident). A handler missing for one of
# THESE specific entity_ids means "the real SensorEntity hasn't (re-)
# registered yet" -- a genuinely transient condition during setup/unload,
# never "this entity_id will never have one" -- so ha_post_state() below
# skips its raw states.async_set() fallback for these rather than writing
# a non-restored ghost state that then collides with the real entity's
# own registration a moment later. Any OTHER entity_id (the standalone/
# cron/addon deployment, or a genuinely not-yet-migrated one) keeps
# today's exact fallback behaviour, unchanged.
_NATIVE_MANAGED_ENTITY_IDS: frozenset[str] = frozenset(
    {
        "sensor.nimbus_solver_battery_forecast",
        "sensor.nimbus_household_load_total_forecast",
        "sensor.nimbus_solver_dispatch_dry_run",
        "sensor.nimbus_mirror_temperature_forecast",
        "sensor.nimbus_mirror_humidity_forecast",
        "sensor.nimbus_solver_quality_report",
        "sensor.nimbus_efficiency_backtest",
        "sensor.nimbus_counterfactual_soc",
        # nimbus issue #1192: these three were MISSING, and the set being
        # incomplete is unambiguously a bug rather than a choice -- this
        # set's own contract, stated above, is "every entity_id this
        # integration calls register_entity_handler() for in native mode",
        # and sensor.py calls it for eleven.
        #
        # The consequence is exactly the ghost this guard exists to
        # prevent: during a reload window the real SensorEntity has not
        # re-registered, so ha_post_state() fell through to a raw
        # states.async_set() for these three, writing a NON-RESTORED state
        # that then reads as a live conflict when the real entity is added
        # a moment later. #1192's own reproduction is the flex family and
        # nothing else, which is the shape this predicts.
        #
        # `test_1192_native_managed_set_covers_every_handler.py` now
        # derives the required set from sensor.py's own
        # register_entity_handler() call sites and fails if this one falls
        # behind again, so the next entity to get a handler cannot be
        # forgotten here the way these three were.
        "sensor.nimbus_flex_signals",
        "sensor.nimbus_flex_report",
        "sensor.nimbus_offer_curve",
        # nimbus issue #495 (Signals 6/7 of #489): the nem-flex-telemetry
        # record. Added in the same change that registers its handler, for
        # the reason #1192 exists -- and it matters more here than for most
        # of this set: this entity's state is a timestamp STRING and its one
        # large attribute is `_unrecorded_attributes`-excluded on the entity
        # class, so the raw states.async_set() fallback would write a state
        # HA's numeric contracts reject and record a payload the entity path
        # deliberately does not.
        "sensor.nimbus_flex_telemetry",
    }
)

# nimbus issue #1396: the managed-entity skip in ha_post_state() below is
# written for a TRANSIENT window -- setup, a reload's unload -- and its own
# comment says a missing handler is "never 'this will never exist.'" That
# assumption can be false permanently: measured on devhub, a
# remote_homeassistant mirror of another install squats the managed
# entity_id, this install's own entity is bumped to a suffixed id, no
# handler ever registers under the managed name, and the skip fires every
# cycle forever. The only trace was a DEBUG line, which is why finding it
# took a dedicated investigation.
#
# A setup/unload window lasts a cycle or two. A streak of consecutive skips
# for the same entity_id well past that is not a window, so it is said once
# at WARNING, and an INFO line marks recovery if a handler later appears.
# Per entity_id, not per process: one squatted id says nothing about the
# others.
_MANAGED_SKIP_WARN_AFTER = 10
_MANAGED_SKIP_STREAK: dict[str, int] = {}
_MANAGED_SKIP_WARNED: set[str] = set()


def _note_managed_publish_skipped(entity_id: str) -> None:
    """Count one skipped publish; warn once when the streak stops looking
    transient. Best-effort: a diagnostic never blocks a publish path."""
    try:
        streak = _MANAGED_SKIP_STREAK.get(entity_id, 0) + 1
        _MANAGED_SKIP_STREAK[entity_id] = streak
        if streak >= _MANAGED_SKIP_WARN_AFTER and entity_id not in _MANAGED_SKIP_WARNED:
            _MANAGED_SKIP_WARNED.add(entity_id)
            _LOGGER.warning(
                "Nimbus #1396: %s has had no registered entity for %d "
                "consecutive solve cycles, so none of them published -- "
                "longer than any setup or reload window. It will keep being "
                "skipped until an entity registers under this exact "
                "entity_id. Check whether another integration occupies the "
                "id (for example a remote_homeassistant mirror, which bumps "
                "this install's own entity to a suffixed id) or whether the "
                "entity has been disabled. Logged once per entity_id.",
                entity_id,
                streak,
            )
    except Exception:  # noqa: BLE001 -- a diagnostic must never break a publish
        _LOGGER.debug("Nimbus #1396: skip-streak bookkeeping failed for %s", entity_id)


def _note_managed_publish_delivered(entity_id: str) -> None:
    """Reset the streak once a handler exists; mark recovery if it had
    been warned about."""
    try:
        _MANAGED_SKIP_STREAK.pop(entity_id, None)
        if entity_id in _MANAGED_SKIP_WARNED:
            _MANAGED_SKIP_WARNED.discard(entity_id)
            _LOGGER.info(
                "Nimbus #1396: %s has a registered entity again and is publishing.",
                entity_id,
            )
    except Exception:  # noqa: BLE001 -- a diagnostic must never break a publish
        _LOGGER.debug("Nimbus #1396: recovery bookkeeping failed for %s", entity_id)


def ha_post_state(entity_id: str, state, attributes: dict) -> None:
    sw = _solver_writer()
    if NATIVE.hass is not None:
        # Dispatch-table shortcut (2026-08-23, issue #55): if a real
        # SensorEntity has registered itself as the handler for this
        # entity_id, route through its own update_from_solver() so HA
        # sees a proper entity update (unique_id, device_info, device_
        # class, unrecorded_attributes) instead of a raw state-machine
        # write. Unregistered entity_ids still fall through to
        # states.async_set() below -- same behaviour as before this
        # seam existed, so any entity that hasn't been migrated yet
        # keeps working exactly as it always has.
        handler = _ENTITY_UPDATE_HANDLERS.get(entity_id)
        # nimbus issue #85 diagnostic (2026-08-23, not yet root-caused):
        # proves/disproves two real candidates in one line -- whether
        # ha_post_state() is being called MORE THAN ONCE per entity per
        # solve cycle at all (which this file's own two known call
        # sites, one each for these entity_ids, shouldn't produce), and
        # whether a call ever falls through to the raw states.async_set
        # fallback for an entity_id that SHOULD have a registered
        # handler (which would mean the handler was unregistered
        # between two calls -- a real, different bug class from #83).
        # nimbus issue #363 (Mark Purcell, codebase review): this used to
        # ALSO print() unconditionally here, on top of the _LOGGER.debug()
        # call below -- but this whole branch only ever executes when
        # _NATIVE_HASS is not None, i.e. ONLY in native (in-process HA
        # integration) mode, never the standalone/cron/addon deployment
        # the removed comment claimed to be keeping it for (that path
        # never sets _NATIVE_HASS at all). A native-mode user reads HA's
        # own logs, not this container's raw stdout, so the print() was
        # pure unconditional noise (8+ lines per cycle) with zero real
        # audience -- _LOGGER.debug() alone already gives the identical
        # trace, correctly gated by log level and visible via HA's own
        # error_log/`logger: default: debug` exactly as issue #85's own
        # original ask wanted ("_LOGGER (not print) for ha_post_state, so
        # it lands in error_log").
        _trace_msg = (
            f"#85 trace: ha_post_state entity_id={entity_id} state={state!r} "
            f"attrs_keys={sorted(attributes.keys()) if attributes else attributes} "
            f"via_handler={handler is not None}"
        )
        _LOGGER.debug(_trace_msg)
        if handler is not None:
            _note_managed_publish_delivered(entity_id)
            NATIVE.hass.add_job(functools.partial(handler, state, attributes))
            return
        # Real root cause of issue #312's residual (2026-09-01, see
        # _NATIVE_MANAGED_ENTITY_IDS' own comment and __init__.py's
        # async_unload_entry() for the full incident): for an entity_id
        # that DOES eventually get a native handler, a missing handler
        # right now means "not (re-)registered yet" -- during setup,
        # during a reload's unload window, or an old trigger that fired
        # in the narrow gap before its own cancellation ran -- never
        # "this will never exist." Writing a raw, non-restored state here
        # would occupy that entity_id in the state machine and make the
        # real entity's OWN registration a moment later collide with it
        # ("does not generate unique IDs... ignoring <entity_id>").
        # Skipping is safe: the real entity, once it registers, publishes
        # fresh data on its own very next solve cycle -- losing one push
        # during a setup/unload transition is a complete non-issue next
        # to a poisoned, permanently-colliding entity_id.
        if entity_id in _NATIVE_MANAGED_ENTITY_IDS:
            _LOGGER.debug(
                "Nimbus: skipping raw states.async_set fallback for %s -- "
                "no native handler registered yet (real entity is mid-"
                "setup/unload, not missing) -- this cycle's update for "
                "this entity is dropped, the next cycle will publish once "
                "the real entity has (re-)registered",
                entity_id,
            )
            _note_managed_publish_skipped(entity_id)
            return
        # states.async_set() mutates HA's own state machine and fires a
        # real event -- unlike the plain dict-read in ha_get() above,
        # this genuinely must happen ON the event loop, never directly
        # from a worker thread. hass.add_job() is HA's own documented
        # thread-safe scheduling primitive for exactly this: safe to call
        # from any thread, correctly hops onto the event loop itself.
        # Also #85 diagnostic: purcell-lab's own follow-up ask on that
        # issue was "a single trace line at every entry to the
        # states.async_set() fallback ... whether or not the dispatch
        # table routes elsewhere" -- via_handler=False above already
        # covers "did we reach the fallback", this WARNING-level line
        # additionally covers "did the fallback actually get scheduled",
        # visible without opting into DEBUG logging first.
        _LOGGER.warning(
            "Nimbus #85 trace: raw states.async_set fallback used for %s "
            "(no registered SensorEntity handler) state=%r attrs_keys=%s",
            entity_id,
            state,
            sorted(attributes.keys()) if attributes else attributes,
        )
        _warn_if_attrs_exceed_recorder_cap(entity_id, attributes)
        NATIVE.hass.add_job(
            functools.partial(
                NATIVE.hass.states.async_set, entity_id, state, attributes
            )
        )
        return
    _warn_if_attrs_exceed_recorder_cap(entity_id, attributes)
    body = json.dumps({"state": state, "attributes": attributes}).encode("utf-8")
    req = urllib.request.Request(
        f"{sw.HA_BASE}/api/states/{entity_id}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {sw._load_token()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


# Real, live household automation design (config/automations.yaml):
# p2p_battery_sell_end_midnight switches the battery to Self-Consume the
# INSTANT the P2P window closes (00:00:00 sharp, no ramp), and
# p2p_haeo_resume_at_4am hands control back at 04:00:00. This is a
# deterministic SWITCH, not something reasoning about marginal profit --
# real, direct household finding (2026-08-22): Nimbus's own shadow plan
# kept discharging for ~30-40 minutes PAST that real cutoff, because the
# existing terminal_value_period_indices mechanism (solver/network.py,
# nimbus repo) is a SOFT economic nudge, and a soft nudge can always be
# outbid if the real export price is still positive enough to look
# marginally profitable. A hard, deterministic real-world rule needs a
# hard LP constraint to match it, not a stronger nudge.
#
# nimbus issue #348 (Mark Purcell, fixed 2026-09-04): the real hour
# count itself is now a wizard field (number.nimbus_solver_post_window_
# self_consume_hours, const.py's CONF_SOLVER_POST_WINDOW_SELF_CONSUME_
# HOURS) -- read via cfg inside fetch_p2p_fixed_export_kw() below, not a
# module constant, so another install's own real self-consume-switch
# timing can be genuinely different from this household's 4h. 4 stays
# here only as the literal default for an install that hasn't set the
# field, matching const.py's own DEFAULT_SOLVER_POST_WINDOW_SELF_
# CONSUME_HOURS -- byte-identical behaviour for every existing install.
P2P_BLOCK_KEYS = (
    (
        "solver_p2p_block_1_rate_kw",
        "solver_p2p_block_1_start_hour",
        "solver_p2p_block_1_end_hour",
    ),
    (
        "solver_p2p_block_2_rate_kw",
        "solver_p2p_block_2_start_hour",
        "solver_p2p_block_2_end_hour",
    ),
    (
        "solver_p2p_block_3_rate_kw",
        "solver_p2p_block_3_start_hour",
        "solver_p2p_block_3_end_hour",
    ),
)


def _configured_p2p_blocks(cfg: dict) -> list[tuple[float, int, int]]:
    """Every configured P2P block as (rate_kw, start_minute, end_minute),
    in local minutes of the day, with the lead time already taken off the
    start (see fetch_p2p_fixed_export_kw() for the lead-time and
    "rate 0 means not configured" rules). One parser, so the plan's block
    window and the block-start solve trigger (p2p_block_start_minutes())
    can never disagree about when a block begins."""
    lead_time_minutes = _cfg_int(cfg, "solver_p2p_block_lead_time_minutes", 0)

    blocks: list[tuple[float, int, int]] = []
    for rate_key, start_key, end_key in P2P_BLOCK_KEYS:
        try:
            rate_kw = _cfg_num(cfg, rate_key, 0.0)
            start_hour = _cfg_int(cfg, start_key, 0)
            end_hour = _cfg_int(cfg, end_key, 0)
        except (TypeError, ValueError):
            continue
        if rate_kw <= 0 or end_hour <= start_hour:
            continue
        start_minute = max(0, start_hour * 60 - lead_time_minutes)
        blocks.append((rate_kw, start_minute, end_hour * 60))
    return blocks


def p2p_block_start_minutes(cfg: dict) -> frozenset[int]:
    """Local minute of the day at which each configured P2P block starts,
    lead time included, e.g. {1019} for a 17:00 block with a 1-minute lead.

    The block-start solve trigger in __init__.py fires a solve in exactly
    these minutes. Without it a block's first minute is only ever planned
    if some other trigger happens to land inside it: the phase-locked
    cron runs at :00:30, :05:30, ..., and the price watcher fires only
    when a price sensor's state actually changes. On the reference
    household that cadence was ~15 s until 3 Oct 2026, when its LocalVolts
    writer moved to the v2 API and stopped republishing an unchanged
    price, and from then the 16:59 lead-time minute was never planned.
    """
    return frozenset(start for _rate, start, _end in _configured_p2p_blocks(cfg))


def fetch_p2p_fixed_export_kw(
    cfg: dict, grid_times: list[datetime]
) -> list[float] | None:
    """Builds the per-period fixed-export-rate array from however many of
    the 3 P2P blocks are actually configured (rate_kw > 0 -- see
    const.py's own comment on CONF_SOLVER_P2P_BLOCK_1_RATE_KW for why 0
    is the "not configured" signal, no separate enable flag needed).
    Returns None (a complete no-op, identical to no P2P scheme at all)
    if every block is unconfigured -- a fresh install with nothing set
    up sees grid_export stay fully LP-optimized against real spot
    prices, exactly as it already does with zero blocks filled in.

    Each grid_time is checked against every configured block's own
    [start_hour, end_hour) range; the first match wins (blocks are
    assumed non-overlapping -- a real household wouldn't configure two
    blocks covering the same hour, not worth defensive-coding against
    on day one, same call already made for the original single-window
    version). end_hour=24 correctly reaches through midnight, since
    Python's own datetime.hour never returns 24 -- any real hour (0-23)
    satisfies `< 24`.

    nimbus issue #565 (real live household observation, 8 Sep ~17:00
    P2P boundary): a P2P block's own rate is a fixed, pre-configured
    $/kWh value with no live-price dependency -- there's no real reason
    the block's own START needs to wait for the literal hour boundary
    the way a spot-price-dependent decision genuinely does. `number.
    nimbus_solver_p2p_block_lead_time_minutes` (shared across all 3
    blocks, 0 default = complete no-op) shifts each block's own
    effective START minute earlier by that many minutes, so the fixed-
    rate window can begin a minute or so before the configured hour,
    absorbing real dispatch-side lag structurally instead of relying on
    tight solve/automation timing. Only the START shifts -- the END
    stays exactly at end_hour*60, unchanged, matching the issue's own
    scope ("begin a minute or so before"). Clamped to not go below
    0 minutes into the calendar day (`max(0, ...)`) -- a block
    configured to start at hour 0 with a nonzero lead time stays a
    same-day-only comparison, same as every other block, rather than
    reaching back into the previous day's own final periods (a real but
    rare edge case, not worth the added wrap-around complexity for a
    "begin a minute early" feature).

    Real, direct fix (2026-08-22, see SELF_CONSUME_HOURS_AFTER_MIDNIGHT_
    CLOSE's own comment above for the full "why a soft nudge alone
    wasn't enough" story): for any block that runs THROUGH midnight
    (end_hour==24), export is ALSO hard-pinned to exactly 0.0kW for the
    following SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE hours -- matching
    the real automation's own deterministic self-consume window, every
    real calendar day this multi-day horizon spans, not just the
    nearest one. 0.0kW export, not "no constraint" -- the real automation
    genuinely stops exporting to the grid at that boundary (self-consume
    still lets the battery cover house load, which is a completely
    separate, still-LP-free decision; only the GRID-EXPORT variable
    itself is pinned here).
    """
    blocks = _configured_p2p_blocks(cfg)

    if not blocks:
        return None

    runs_through_midnight = any(
        end_minute == 24 * 60 for _rate_kw, _start_minute, end_minute in blocks
    )
    self_consume_hours = _cfg_int(cfg, "solver_post_window_self_consume_hours", 4)

    result: list[float] = []
    for gt in grid_times:
        gt_minute = _local(gt).hour * 60 + _local(gt).minute
        matched_rate = float("nan")
        for rate_kw, start_minute, end_minute in blocks:
            if start_minute <= gt_minute < end_minute:
                matched_rate = rate_kw
                break
        if runs_through_midnight and _local(gt).hour < self_consume_hours:
            matched_rate = 0.0
        result.append(matched_rate)
    return result


def p2p_blocks_daily_energy_kwh(cfg: dict) -> float:
    """kWh the household's configured P2P blocks commit to per day, the sum
    of rate_kw x (end_hour - start_hour) over every configured block, and
    0.0 when none is.

    The P2P volume cap's default when no settlement history says otherwise
    (nimbus #1537). A block is a fixed commitment, a locked window at a
    locked kW delivered every day, so its own energy is the volume the
    household sells as P2P. Before this, an install with blocks but no
    settlement sensor got this repository's reference household's 60 kWh
    (`P2P_RECENT_AVG_VOLUME_FALLBACK_KWH`), and a generic-branch install
    left at the wizard's 0 got no P2P volume at all. The lead time is left
    out: it moves a block's start by minutes, not its committed energy.
    """
    total = 0.0
    for rate_key, start_key, end_key in P2P_BLOCK_KEYS:
        try:
            rate_kw = _cfg_num(cfg, rate_key, 0.0)
            start_hour = _cfg_int(cfg, start_key, 0)
            end_hour = _cfg_int(cfg, end_key, 0)
        except (TypeError, ValueError):
            continue
        if rate_kw <= 0 or end_hour <= start_hour:
            continue
        total += rate_kw * (end_hour - start_hour)
    return total


def p2p_bonus_price_by_period(
    bonus_rate: float, fixed_export_kw: list[float] | None, n_periods: int
) -> np.ndarray:
    """The settled P2P rate, offered ONLY in the periods where the
    household actually has a P2P commitment -- and zero everywhere else.

    A P2P premium is not a property of the day, it is a property of the
    BLOCK. The household's scheme settles inside their committed window
    (17:00-24:00 local here); an hour outside it earns plain spot and
    nothing more. Spreading the day's settled $/kWh flat across all 24
    hours hands the oracle money it could not have earned, and the
    oracle -- being an oracle -- spends the day arranging to collect it.

    Confirmed on real devhub data, 2026-09-17, scoring 15 Sep: with a
    flat bonus of $0.2183/kWh (the day's real $10.4032 / 47.668 kWh)
    `j_star` charged the battery at 01:00-02:00 local paying $0.215/kWh
    and dumped 20 kW to grid at 05:00 local into a spot price of
    $0.088/kWh. That is a 13c/kWh loss on its face and no optimiser
    takes it voluntarily -- it only clears because the phantom premium
    turns $0.088 into $0.306. Every dollar of that trade inflates
    `j_star`, and regret is measured against `j_star`.

    The same `grid_oracle` is reused for `j_ref` pricing (#1026), so the
    reference plane was being credited the premium too -- on 15 Sep for
    ~41 kWh of MIDDAY SOLAR export, none of it inside the window. That
    lifts `j_ref` toward `j_ach` and shrinks the numerator. Both ends of
    the EPR fraction moved, both in the direction that depresses it.

    `fixed_export_kw` is the honest window signal and it is already
    built from the household's own configured blocks by
    fetch_p2p_fixed_export_kw() -- non-zero exactly where a commitment
    exists, and explicitly 0.0 through the post-midnight self-consume
    hours where export is pinned off entirely. Reusing it means this
    gate cannot drift away from the window the LP is actually pinned to,
    which a second reading of the block hours here certainly would.

    `fixed_export_kw is None` means no block is configured at all. There
    is then no window information to gate on, so the flat rate is kept:
    that is the pre-existing behaviour and this must not make an install
    with no configured P2P block worse on the strength of a guess.
    """
    if fixed_export_kw is None:
        return np.full(n_periods, bonus_rate)
    return np.where(
        np.asarray(fixed_export_kw, dtype=np.float64) > 0.0, bonus_rate, 0.0
    )


def fetch_entity_went_unavailable(
    entity_id: str, after: datetime, end: datetime
) -> bool | None:
    """Did this entity pass through a NON-numeric state (`unavailable`,
    `unknown`, ...) in `(after, end]`? True/False, or None when the recorder
    could not be read (nimbus issue #1477).

    `fetch_entity_history_range()` drops non-numeric rows, which is right for
    its callers but leaves the coverage gate unable to tell two opposite
    things apart once a series stops early:

    * **a held value** -- HA's recorder writes a row only when a state
      CHANGES, so a PV sensor sitting at 0.0 from dusk to midnight writes
      nothing after its last change. Measured on a real install: last row
      `0.0` at 17:39:23, nothing more that day, while sibling sensors kept
      changing. The sensor was fine; it was night.
    * **a real outage** -- the integration drops out and HA records the
      entity as `unavailable`.

    Only the raw states can separate them, so this reads them unfiltered.
    """
    sw = _solver_writer()
    raw: list[str] = []
    if NATIVE.hass is not None:
        try:
            import asyncio

            from homeassistant.components.recorder import (
                get_instance as _recorder_get_instance,
            )
            from homeassistant.components.recorder import history as _recorder_history

            async def _fetch() -> dict:
                return await _recorder_get_instance(NATIVE.hass).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    NATIVE.hass,
                    after,
                    end,
                    entity_id,
                    True,  # no_attributes
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), NATIVE.hass.loop)
            states = future.result(timeout=30).get(entity_id, [])
            raw = [s.state for s in states if s.last_changed > after]
        except Exception:
            _LOGGER.debug(
                "Nimbus Solver: fetch_entity_went_unavailable(%s) recorder read failed",
                entity_id,
                exc_info=True,
            )
            return None
    else:
        url = (
            f"{sw.HA_BASE}/api/history/period/"
            f"{after.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
            f"?filter_entity_id={entity_id}"
            f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
        )
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {sw._load_token()}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            return None
        for p in data[0] if data and data[0] else []:
            when = parse_iso(p.get("last_changed")) if p.get("last_changed") else None
            if when is not None and when > after:
                raw.append(p.get("state"))
    for state in raw:
        try:
            float(state)
        except (TypeError, ValueError):
            return True
    return False


def fetch_entity_history_range(
    entity_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Real recorded numeric history for one entity over an EXPLICIT
    [start, end) window -- generalizes fetch_price_history()'s own
    "last N days up to right now" shape (this file's other history
    fetch) into an explicit day-window fetch, needed by
    compute_daily_quality_report() to score one specific, already-
    elapsed calendar day rather than a rolling recent window. Same
    REST/native dual-mode split, same "degrade to [] on any failure,
    never crash" discipline as every other real-data fetch in this
    file.
    """
    sw = _solver_writer()
    if NATIVE.hass is not None:
        try:
            import asyncio

            from homeassistant.components.recorder import (
                get_instance as _recorder_get_instance,
            )
            from homeassistant.components.recorder import history as _recorder_history

            async def _fetch() -> dict:
                return await _recorder_get_instance(NATIVE.hass).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    NATIVE.hass,
                    start,
                    end,
                    entity_id,
                    True,  # no_attributes
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), NATIVE.hass.loop)
            changes = future.result(timeout=30)
            states = changes.get(entity_id, [])
        except Exception:
            # nimbus issue #363 (Mark Purcell, codebase review): degrade
            # stays, breadcrumb added -- same reasoning as fetch_price_
            # history()'s own identical recorder-bridge except above.
            _LOGGER.debug(
                "Nimbus Solver: fetch_entity_history_range(%s) recorder read failed",
                entity_id,
                exc_info=True,
            )
            return []
        out: list[tuple[datetime, float]] = []
        for s in states:
            try:
                v = float(s.state)
            except (TypeError, ValueError):
                continue
            out.append((s.last_changed.astimezone(LOCAL_TZ), v))
        return sorted(out, key=lambda x: x[0])
    url = (
        f"{sw.HA_BASE}/api/history/period/{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {sw._load_token()}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    if not data or not data[0]:
        return []
    out = []
    for p in data[0]:
        state = p.get("state")
        if state in (None, "unknown", "unavailable"):
            continue
        try:
            v = float(state)
        except (TypeError, ValueError):
            continue
        out.append((parse_iso(p["last_changed"]).astimezone(LOCAL_TZ), v))
    return sorted(out, key=lambda x: x[0])


def fetch_entity_attribute_history_range(
    entity_id: str, attribute: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """nimbus issue #592: same shape as fetch_entity_history_range()
    just above, for the real case that function can't cover -- a
    water_heater/climate done_entity's own STATE is a mode string
    ("eco"/"performance"), never a number; the value that matters
    (current_temperature) lives on the ATTRIBUTE instead (see
    done_condition.py's own read_current_temperature() for the live-
    read equivalent of this same fact). Fetches WITH attributes (unlike
    fetch_entity_history_range()'s own no_attributes=True), reads
    `attribute` off each historical state instead of the state itself.
    Same dual native/REST mode, same degrade-to-[]-on-any-failure
    discipline as every other real-data fetch in this file.
    """
    sw = _solver_writer()
    if NATIVE.hass is not None:
        try:
            import asyncio

            from homeassistant.components.recorder import (
                get_instance as _recorder_get_instance,
            )
            from homeassistant.components.recorder import history as _recorder_history

            async def _fetch() -> dict:
                return await _recorder_get_instance(NATIVE.hass).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    NATIVE.hass,
                    start,
                    end,
                    entity_id,
                    False,  # no_attributes -- must be False, the whole point here
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), NATIVE.hass.loop)
            changes = future.result(timeout=30)
            states = changes.get(entity_id, [])
        except Exception:
            _LOGGER.debug(
                "Nimbus Solver: fetch_entity_attribute_history_range(%s, %s) "
                "recorder read failed",
                entity_id,
                attribute,
                exc_info=True,
            )
            return []
        out: list[tuple[datetime, float]] = []
        for s in states:
            raw = s.attributes.get(attribute)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            out.append((s.last_changed.astimezone(LOCAL_TZ), v))
        return sorted(out, key=lambda x: x[0])
    url = (
        f"{sw.HA_BASE}/api/history/period/{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {sw._load_token()}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    if not data or not data[0]:
        return []
    out = []
    for p in data[0]:
        raw = (p.get("attributes") or {}).get(attribute)
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        out.append((parse_iso(p["last_changed"]).astimezone(LOCAL_TZ), v))
    return sorted(out, key=lambda x: x[0])


def resample_history_nearest(
    pts: list[tuple[datetime, float]],
    grid_times: list[datetime],
    default: float = 0.0,
    *,
    backfill_first: bool = False,
) -> list[float]:
    """Nearest-at-or-before lookup against real recorded history points
    -- same convention as resample_forecast() elsewhere in this file,
    just against (datetime, value) tuples from fetch_entity_history_
    range() instead of a forecast's own {time, value} dicts.

    What happens when NOTHING in `pts` precedes a given `gt` is an
    explicit, per-caller choice, because the physically correct answer
    genuinely differs by signal type (the same FLOW-vs-STATE split
    resample_history_mean() below already documents):

    - `backfill_first=False` (default): return `default`. Correct for
      power-FLOW signals (solar/load/battery/EV pack power) and for
      boolean availability masks -- absence of data does NOT mean
      "whatever this sensor read the next time it woke up."
    - `backfill_first=True`: return the earliest real sample instead
      (`pts[0][1]`), falling back to `default` only when `pts` is
      genuinely empty. Correct for STATE signals (SoC %, prices), where
      the first real reading of the window is a far better estimate of
      the unobserved earlier state than a hardcoded constant -- an EV
      parked overnight really did sit at its wake-up SoC the whole time.

    nimbus issue #843 (Mark Purcell, root-caused against real household
    recorder data): this function previously ALWAYS initialised `val` to
    `pts[0][1]`, i.e. it silently behaved as `backfill_first=True` for
    every caller, and could therefore return a value recorded AFTER `gt`
    from a function whose whole contract is "at or before" -- never once
    falling through to the `default` its callers explicitly passed.

    Confirmed live impact: an EV whose telemetry genuinely sleeps
    overnight (zero recorder rows 00:00 -> 08:51, real Tesla sleep
    behaviour) had every hour from 00:00-07:00 inherit its first
    post-wake reading. That reading also happened to be a ~1000x-wrong
    W-vs-kW boot transient (a separate, still-open half of #843), so the
    quality report published a ~1,500 kW achieved battery power -- about
    19x the household's real physical fleet ceiling -- for eight
    straight hours, making EPR/regret unusable for that day. The unit
    bug corrupted one sample; THIS bug is what smeared it across a third
    of the day.
    """
    out = []
    for gt in grid_times:
        val = pts[0][1] if (pts and backfill_first) else default
        for t, v in pts:
            if t <= gt:
                val = v
            else:
                break
        out.append(float(val))
    return out


def resample_history_mean(
    pts: list[tuple[datetime, float]],
    grid_times: list[datetime],
    period_hours: float,
    default: float = 0.0,
) -> list[float]:
    """Period-mean lookup against real recorded history points -- averages
    every real sample falling within [grid_time, grid_time + period_hours)
    for each grid point, instead of resample_history_nearest()'s single
    nearest-at-or-before instant.

    nimbus issue #428 (Mark Purcell): confirmed live that a brief, real,
    isolated telemetry spike right at a period boundary (a genuine
    +20.9kW battery sample, seconds wide, against that hour's real mean
    of -2.66kW) gets picked up WHOLE by resample_history_nearest() --
    treated as representative of the entire 15-minute period, flipping
    that period's reconstructed grid direction relative to what a real
    energy-weighted average of the period would show (traced end to end
    against Mark's own real recorder data: the spike alone reconstructs
    to a large positive/importing grid figure, while the real hourly
    mean reconstructs to negative/exporting -- matching the real meter).
    compute_daily_quality_report()'s achieved (J_ach) trajectory
    reconstructs real energy flow (solar/load/battery power) from
    history -- an energy-weighted mean over the period is the physically
    correct way to do that (matches how a real meter integrates power
    into energy over that quarter-hour), not an instantaneous point
    sample of whatever the sensor happened to read at the exact grid
    instant.

    Falls back to resample_history_nearest()'s own nearest-at-or-before
    pick when a period genuinely has zero real samples inside its own
    window (sparse/low-frequency history) -- never fabricates a gap.

    Deliberately scoped to the three power-FLOW signals (solar/load/
    battery) feeding compute_daily_quality_report()'s achieved
    reconstruction -- SoC and price are real STATE values (sample-and-
    hold is the physically correct model for those, not an average),
    and every other caller of resample_history_nearest() is unchanged.
    """
    out = []
    for gt in grid_times:
        window_end = gt + timedelta(hours=period_hours)
        rows = [(ts, v) for ts, v in pts if gt <= ts < window_end]
        if rows:
            # nimbus issue #1008: TIME-WEIGHTED, not a plain average of
            # samples.
            #
            # HA's recorder stores state CHANGES, so samples are
            # irregularly spaced -- dense while a value is moving, sparse
            # while it holds. `sum(vals) / len(vals)` therefore weights a
            # 2-second spike exactly as heavily as a 40-minute plateau,
            # and a battery that jumps to +-40 kW and then sits flat
            # writes a row for every step of the spike and almost none
            # for the plateau.
            #
            # Measured on the reference household, 2026-09-15:
            #
            #   scorer, sample mean   ->  99.675 kWh in / 107.428 out
            #                             = -7.75 kWh net (net DISCHARGE)
            #   inverter counters     -> 106.6   kWh in / 100.9   out
            #                             = +5.70 kWh net (net CHARGE)
            #   HA's own statistics   -> mean -0.2229 kW x 24 h
            #                             = -5.35 kWh  (net CHARGE)
            #
            # HA's statistics mean IS time-weighted, which is why it
            # agrees with the counters to 0.35 kWh while this function
            # disagreed by 13.5 kWh and inverted the day's direction --
            # the reconstructed SoC ran 16.07% -> 0.44% on a day the pack
            # really went 17.4% -> ~20%.
            #
            # Each sample is weighted by how long it HELD: until the next
            # sample, or the end of the window for the last one. The
            # first sample's weight starts at the window boundary rather
            # than at its own timestamp, because whatever value preceded
            # it is carried in by the `else` branch's semantics below --
            # a recorded state holds until the next one replaces it.
            total = 0.0
            weighted = 0.0
            for i, (ts, v) in enumerate(rows):
                next_ts = rows[i + 1][0] if i + 1 < len(rows) else window_end
                span = (next_ts - ts).total_seconds()
                if span <= 0:
                    continue
                weighted += v * span
                total += span
            # Time between the window start and the first recorded sample
            # is held by the last REAL value before the window -- the same
            # last-known-value model resample_history_nearest() applies.
            #
            # Only when such a sample genuinely exists. If nothing
            # precedes the window there is no observation to carry in,
            # and weighting the gap at `default` would INVENT data inside
            # a period that has real samples -- biasing every scored
            # day's first period, since the history fetch starts at the
            # window boundary. A period with no samples at all still
            # falls through to the `else` branch below, which is where
            # the flow-signal "absent power reads as 0" convention
            # belongs.
            prior_rows = [v for ts, v in pts if ts < gt]
            lead_span = (rows[0][0] - gt).total_seconds()
            if lead_span > 0 and prior_rows:
                weighted += prior_rows[-1] * lead_span
                total += lead_span
            out.append(weighted / total if total > 0 else rows[0][1])
        else:
            out.append(resample_history_nearest(pts, [gt], default=default)[0])
    return out


def _kw_scale_factor(entity_id: str) -> float:
    """Real bug found live on devhub 2026-08-28: compute_daily_quality_
    report() and compute_efficiency_backtest_report() both take a
    configured *_power_sensor entity_id and treat its raw historical
    values as already being kW, with no check against what the entity
    itself actually declares. A household pointing solver_solar_power_
    sensor at a native Watts sensor (very common for raw inverter/logger
    telemetry -- confirmed live: sensor.combined_total_dc_power's own
    unit_of_measurement is "W") silently fed solar values ~1000x too
    large into both reports, producing nonsense economics (confirmed
    live: theoretical_maximum_yield/regret_dollars around -$1280/-$1289
    for one real household-day, an impossible magnitude).

    Returns the multiplier to bring a history value into kW: 0.001 for a
    "W" sensor, 1.0 for "kW" or anything else (including no unit at all,
    or a lookup failure) -- 1.0 is the correct default since "kW" was
    always the original, undocumented assumption these two functions
    made; this only corrects the one real, confirmed-live mismatch
    (Watts), not a guess at every possible unit HA's power device class
    could report.
    """
    try:
        unit = ha_get(entity_id).get("attributes", {}).get("unit_of_measurement")
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return 1.0
    # nimbus #1570: the shared converter (W/kW/MW/GW, any case).
    return power_scale_to_kw(unit)


@functools.cache
def _nimbus_version() -> str | None:
    """This package's own `manifest.json` version, or None.

    Read from disk rather than taken from the config entry, because this
    module runs in BOTH deployment shapes: natively inside HA, where the
    entity layer supplies `sw_version`, and in the standalone/cron writer,
    where there is no entity layer and no `hass` at all (see this module's
    own header for the dual-import boundary). `manifest.json` sits beside
    this file in both, so it is the one source available to each.

    Returns None rather than raising on any failure -- a missing or
    malformed manifest must never cost a day's score. An unstamped row
    then reads exactly as every row written before this change did, which
    is the correct fallback: "scored by something that did not say".
    """
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")
        with open(path, encoding="utf-8") as handle:
            version = json.load(handle).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and version else None


def _version_stamp() -> dict[str, str]:
    """`{"nimbus_version": <this release>}`, or `{}` when unknown.

    **Why a dict to splat rather than a plain value** (nimbus issue #1256).
    `_nimbus_version()` can return None, and an explicit
    `"nimbus_version": None` is worse than an absent key: the entity layer's
    own #972 fallback declines to overwrite a key that is already present, so
    a None would publish through as None and suppress the fallback that would
    otherwise have said something true. An absent key reads exactly as every
    row written before #1120 did -- "produced by something that did not say" --
    which is this module's own established convention for an unknown version.

    **Why the publishers stamp at all, when #972 already adds one.** They are
    answering different questions with the same attribute name, and #1256 is
    what happens when the difference is not noticed:

    * #972's entity-level stamp describes the RUNNING install. That is right
      for its own purpose, which is mirror detection -- a `nimbus_version`
      disagreeing with the version just deployed proves the entity_id
      resolved to another install's sensor.
    * #1120/#1219's row-level stamp describes the release that COMPUTED the
      figures. That is what a reader of a once-a-day report needs.

    The four daily publishers restore their previous attributes across a
    restart (`_RESTORE_ACROSS_RESTART = True`), so for them the two answers
    diverge the moment a release is deployed: the figures are yesterday's, the
    running install is today's. Measured on production 2026-09-26 -- a deploy
    at 10:38 AEST left all four sensors reading `nimbus_version: 0.94.420`
    against `generated_at` values of 00:00, 00:00, 00:00:13 and 06:06, every
    one of them computed by v0.94.417. The same quality report had read
    `0.94.417` correctly a few hours earlier, so the stamp moved without the
    figures being recomputed.

    That is not cosmetic. It read as "the #1233 fix shipped in .420 and did
    not work" when the truth was "these are .417's numbers wearing .420's
    label" -- and this project's own standing lesson is that *a version
    difference is not an explanation for a wrong number until the diff is
    actually read*. A stamp that lies makes that check harder, not easier.

    Stamping here makes the claim travel with the computation, so a restore
    reproduces it unchanged alongside `generated_at`.
    """
    version = _nimbus_version()
    return {"nimbus_version": version} if version is not None else {}


# nimbus issue #1302 (spec 003 step 2): moved here verbatim from
# solver_writer.py. Fully self-contained apart from _LOGGER, which this
# module owns -- so no qualification was needed and the body is byte-for-
# byte what it was.
def safe_num(entity_id: str, fallback: float = 0.0) -> float:
    """Read entity_id's current state as a float, degrading gracefully
    (WARN + fallback) instead of crashing the whole solve cycle when the
    entity's real state can't be parsed as a number.

    Real, live crash this fixes (Mark Purcell, 2026-08-24, direct
    follow-up to #58's own "it should catch errors and manage them"
    complaint -- see resolve_min_soc_kwh() above for the other half of
    that same conversation): a configured solver_export_price_sensor
    entity's real state came back as '2026-08-24T13:00:00+10:00' (a
    timestamp, not a price) -- the bare, unprotected
    ``float(ha_get(entity_id)["state"])`` this replaces (previously a
    small closure named ``num()``, local to main() and therefore
    untestable in isolation -- extracted here for the same reason as
    resolve_max_discharge_kw()/resolve_min_soc_kwh() above) had no
    defence at all against that shape, and crashed every single solve
    cycle it was reached on. Same class of external-read that "might
    not be shaped as expected" already handled this way elsewhere in
    this file (resolve_max_discharge_kw()'s own malformed-'max'-
    attribute handling).

    0.0 (the default fallback) matches this file's own established "no
    better default exists" convention for a portable/generic install
    with genuinely missing data (the P2P bonus fields, flat fee rate,
    etc. all default the same way). Used for the three real, required
    scalar-entity reads in main(): solver_import_price_sensor's and
    solver_export_price_sensor's own scalar-fallback branch (only
    reached when no forecast array exists at all), and
    solver_battery_soc_sensor's live SoC read -- the latter is doubly
    protected even on a 0.0 fallback: the existing initial_soc_kwh clamp
    immediately below always has a strictly-positive floor to clamp
    into now, thanks to resolve_min_soc_kwh() above.
    """
    try:
        return float(ha_get(entity_id)["state"])
    except (KeyError, TypeError, ValueError) as e:
        _LOGGER.warning(
            "Nimbus Solver: entity '%s' has a non-numeric state -- could "
            "not parse it as a price/SoC value (%s). Falling back to %s "
            "for this solve. Check that this entity is genuinely "
            "configured correctly (a real price/SoC sensor, not "
            "something else that happens to share the name).",
            entity_id,
            e,
            fallback,
        )
        return fallback
