"""nimbus issue #1310: the fixture inputs shared by the pre-extraction
reference capture (capture.py, run against commit
0d0d553a22e8e4d5db2339ede13337d1645fa220) and the post-extraction
regression test (tests/test_1310_battery_fleet_characterization.py, run
against current main's solver_inputs/extra_batteries.py and
solver_inputs/battery_participants.py).

This module is deliberately self-contained (no import of solver_inputs
itself, no reliance on which "shape" the target functions live in) so
the exact same file can be copied unmodified into a worktree checked out
at the reference revision to regenerate reference.json -- see capture.py's
own module docstring for the full reproduction command.

Two scenarios, matching #1310's own acceptance criteria:

  build_extra_batteries() -- the LIVE, forward-looking config builder.
  Two battery_participant subentries ("ev_m3p" home, "ev_my" away),
  covering: complete BatteryConfig fields, ordered participant list,
  live availability gating (available=False -> unavailable_until_
  period_index), and departure-deadline resolution into a real period
  index (no widening on this side -- see elements.BatteryConfig's own
  docstring, build_extra_batteries has nothing to widen against yet).

  _resolve_battery_participant_history() -- the RETROSPECTIVE, history-
  based sibling used by the quality scorer. Same two participants,
  reconstructed from real recorder history instead of live state:
  charge/discharge arrays, final SoC, an availability window that
  actually zeroes a real nonzero reading (nimbus #467/#768), a
  departure-deadline widened down to what the day actually reached
  (nimbus #1111), and a shared 15 kW garage charger whose configured cap
  is exceeded once BOTH participants' real charge+discharge draw is
  combined -- confirming nimbus #1109's widening reaches the oracle AND
  nimbus #1140's fix (discharge is not excluded from that widening) is
  still in effect through the FULL function, not just in isolation
  against _widen_shared_charger_cap_to_achieved() directly (which
  tests/test_1140_shared_charger_widening_ignores_discharge.py already
  covers).

nimbus issue #1336 (found while building this fixture, filed separately,
not fixed here): _participant_departure_deadline(), when reached through
_resolve_battery_participant_history(), is handed a SCALAR period_hours
and indexes it as a list once a participant's resolved deadline period
index is > 0 -- a real, pre-existing bug in BOTH the reference revision
and current main, reproduced directly against the reference commit while
building this fixture. EV1's departure_hour below is deliberately set to
grid_times[0]'s own local hour (index 0) so the buggy loop
(range(min(idx, len(actual_charge_kw)))) never executes and this
fixture's own output stays a meaningful characterization of the
widen-down-to-achieved rule rather than "both sides raise the same
exception." Fixing #1336 is explicitly out of scope for this
test-only PR.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
from solver import elements

BRISBANE = ZoneInfo("Australia/Brisbane")

# ---------------------------------------------------------------------------
# Shared fixture plumbing (same shapes tests/test_solver_writer_battery_
# participants.py and tests/test_solver_writer_battery_participant_history.py
# already use).
# ---------------------------------------------------------------------------


def _fake_subentry(subentry_id: str, subentry_type: str, data: dict):
    return SimpleNamespace(
        subentry_id=subentry_id, subentry_type=subentry_type, data=data
    )


def _fake_state(value, entity_id: str):
    return SimpleNamespace(entity_id=entity_id, state=value, attributes={})


def _fake_native_hass(subentries: list, states: dict | None = None):
    entry = SimpleNamespace(subentries={s.subentry_id: s for s in subentries})
    resolved_states = {
        eid: SimpleNamespace(entity_id=eid, state=s.state, attributes=s.attributes)
        for eid, s in (states or {}).items()
    }
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
        states=SimpleNamespace(get=lambda eid: resolved_states.get(eid)),
    )


def _flat_history(value: float, start: datetime, end: datetime, step_minutes: int = 15):
    out = []
    t = start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _hourly_history(
    values_by_hour: dict[int, float], day_start: datetime, default: float = 0.0
):
    """One real sample per hour, every hour -- matches
    test_solver_writer_battery_participant_history.py's own
    test_driving_discharge_while_away_is_zeroed pattern, and keeps every
    period trustworthy under _stale_power_period_indices() (#1161) since
    each grid period gets a sample of its own, deliberately out of scope
    for this fixture."""
    return [
        (day_start + timedelta(hours=h), values_by_hour.get(h, default))
        for h in range(24)
    ]


def _hourly_state_history(off_hours: set[int], day_start: datetime):
    return [
        (day_start + timedelta(hours=h), "off" if h in off_hours else "on")
        for h in range(24)
    ]


# ---------------------------------------------------------------------------
# Scenario 1: build_extra_batteries() -- the live, forward-looking path.
# ---------------------------------------------------------------------------

_BUILD_PERIODS_START = datetime(2026, 9, 20, 0, 0, tzinfo=BRISBANE)
BUILD_PERIODS = elements.PeriodGrid(hours=np.ones(24), start=_BUILD_PERIODS_START)

_BUILD_EV1_DATA = {
    "battery_participant_name": "ev_m3p",
    "battery_participant_capacity_kwh": 60.0,
    "battery_participant_soc_sensor": "sensor.m3p_soc",
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 11.0,
    "battery_participant_min_soc_percent": 20.0,
    "battery_participant_max_soc_percent": 95.0,
    "battery_participant_efficiency_percent": 92.0,
    "battery_participant_available_entity": "binary_sensor.m3p_home",
    "battery_participant_departure_hour": 7,
    "battery_participant_must_have_soc_by_departure_percent": 80.0,
    "battery_participant_shared_charger_group": "garage",
    "battery_participant_shared_charger_max_kw": 15.0,
    "battery_participant_degradation_cost_per_kwh": 0.002,
    "battery_participant_salvage_value": 0.05,
}

_BUILD_EV2_DATA = {
    "battery_participant_name": "ev_my",
    "battery_participant_capacity_kwh": 75.0,
    "battery_participant_soc_sensor": "sensor.my_soc",
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 10.0,
    "battery_participant_min_soc_percent": 15.0,
    "battery_participant_max_soc_percent": 90.0,
    "battery_participant_efficiency_percent": 90.0,
    "battery_participant_available_entity": "binary_sensor.my_home",
    "battery_participant_shared_charger_group": "garage",
    "battery_participant_shared_charger_max_kw": 15.0,
    "battery_participant_degradation_cost_per_kwh": 0.0015,
    "battery_participant_salvage_value": 0.0,
}


def build_extra_batteries_native_hass():
    """ev_m3p home (available='on'), ev_my away (available='off') --
    real, ordered two-participant fleet for build_extra_batteries()."""
    return _fake_native_hass(
        [
            _fake_subentry("s1", "battery_participant", _BUILD_EV1_DATA),
            _fake_subentry("s2", "battery_participant", _BUILD_EV2_DATA),
        ],
        states={
            "sensor.m3p_soc": _fake_state("42.0", "sensor.m3p_soc"),
            "sensor.my_soc": _fake_state("58.0", "sensor.my_soc"),
            "binary_sensor.m3p_home": _fake_state("on", "binary_sensor.m3p_home"),
            "binary_sensor.my_home": _fake_state("off", "binary_sensor.my_home"),
        },
    )


def run_build_extra_batteries(sw_module, build_extra_batteries_fn) -> list:
    """Runs build_extra_batteries_fn(periods=...) against sw_module's own
    _NATIVE_HASS seam, restoring it afterwards. `build_extra_batteries_fn`
    is passed in explicitly so the SAME scenario drives either the
    pre-extraction solver_writer.build_extra_batteries or the
    post-extraction solver_inputs.extra_batteries.build_extra_batteries."""
    original = sw_module.NATIVE.hass
    sw_module.NATIVE.hass = build_extra_batteries_native_hass()
    try:
        return build_extra_batteries_fn(periods=BUILD_PERIODS)
    finally:
        sw_module.NATIVE.hass = original


# ---------------------------------------------------------------------------
# Scenario 2: _resolve_battery_participant_history() -- the retrospective,
# recorder-history-based sibling used by the quality scorer.
# ---------------------------------------------------------------------------

DAY_START = datetime(2026, 9, 20, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)
GRID_TIMES = [DAY_START + timedelta(hours=i) for i in range(24)]
PERIOD_HOURS = 1.0
N_PERIODS = 24

_HISTORY_EV1_DATA = {
    "battery_participant_name": "ev_m3p",
    "battery_participant_capacity_kwh": 60.0,
    "battery_participant_soc_sensor": "sensor.m3p_soc",
    "battery_participant_power_sensor": "sensor.m3p_power",
    "battery_participant_power_positive_is_charge": True,
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 11.0,
    "battery_participant_min_soc_percent": 20.0,
    "battery_participant_max_soc_percent": 95.0,
    "battery_participant_efficiency_percent": 92.0,
    "battery_participant_available_entity": "binary_sensor.m3p_home",
    # Deliberately grid_times[0]'s own local hour (0) -- see this module's
    # own docstring on nimbus #1336 for why idx must stay 0 here.
    "battery_participant_departure_hour": 0,
    "battery_participant_must_have_soc_by_departure_percent": 80.0,
    "battery_participant_shared_charger_group": "garage",
    "battery_participant_shared_charger_max_kw": 15.0,
    "battery_participant_degradation_cost_per_kwh": 0.002,
}

_HISTORY_EV2_DATA = {
    "battery_participant_name": "ev_my",
    "battery_participant_capacity_kwh": 75.0,
    "battery_participant_soc_sensor": "sensor.my_soc",
    "battery_participant_power_sensor": "sensor.my_power",
    "battery_participant_power_positive_is_charge": True,
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 12.0,
    "battery_participant_min_soc_percent": 15.0,
    "battery_participant_max_soc_percent": 90.0,
    "battery_participant_efficiency_percent": 90.0,
    "battery_participant_available_entity": "binary_sensor.my_home",
    "battery_participant_shared_charger_group": "garage",
    "battery_participant_shared_charger_max_kw": 15.0,
    "battery_participant_degradation_cost_per_kwh": 0.0018,
}

# EV2's real away window -- a genuine mid-morning trip.
_EV2_AWAY_HOURS = {9, 10, 11}


def history_native_hass():
    return _fake_native_hass(
        [
            _fake_subentry("s1", "battery_participant", _HISTORY_EV1_DATA),
            _fake_subentry("s2", "battery_participant", _HISTORY_EV2_DATA),
        ]
    )


def history_power_fetch(entity_id: str, start: datetime, end: datetime):
    if entity_id == "sensor.m3p_power":
        # Charges at 8 kW (raw, power_positive_is_charge=True) for hour
        # 18 only -- the single period that overlaps ev_my's own
        # discharge below, to make the shared-charger peak land there.
        return _hourly_history({18: 8.0}, DAY_START)
    if entity_id == "sensor.my_power":
        # Discharges (V2H) at 12 kW during hour 18 (home, not away) and
        # at 3 kW during hour 10 -- inside its own away window, so the
        # availability mask must zero this specific, real, nonzero
        # reading rather than the test only ever masking zeros.
        return _hourly_history({18: -12.0, 10: -3.0}, DAY_START)
    return []


def history_soc_fetch(entity_id: str, start: datetime, end: datetime):
    if entity_id == "sensor.m3p_soc":
        return _flat_history(35.0, start, end)
    if entity_id == "sensor.my_soc":
        return _flat_history(60.0, start, end)
    return []


def history_state_fetch(entity_id: str, start: datetime, end: datetime):
    if entity_id == "binary_sensor.m3p_home":
        return _hourly_state_history(set(), DAY_START)  # home all day
    if entity_id == "binary_sensor.my_home":
        return _hourly_state_history(_EV2_AWAY_HOURS, DAY_START)
    return []


def history_fetch(entity_id: str, start: datetime, end: datetime):
    """Backs BOTH fetch_entity_history_range() (SoC) and
    fetch_entity_power_history_kw() (power) -- same one-fixture-covers-
    both-reads convention test_solver_writer_battery_participant_
    history.py's own _patch_history() already documents, since #843
    option A only changed what's asked for, not the (datetime, kW/pct)
    shape that comes back."""
    power = history_power_fetch(entity_id, start, end)
    if power:
        return power
    return history_soc_fetch(entity_id, start, end)


def run_resolve_history(sw_module, resolve_fn) -> list:
    """Runs resolve_fn(...) against sw_module's own recorder-history
    seams, restoring every patched attribute afterwards."""
    original_hass = sw_module.NATIVE.hass
    original_history = sw_module.fetch_entity_history_range
    original_power = sw_module.fetch_entity_power_history_kw
    original_state = sw_module.fetch_entity_state_history_range
    sw_module.NATIVE.hass = history_native_hass()
    sw_module.fetch_entity_history_range = history_fetch
    sw_module.fetch_entity_power_history_kw = history_fetch
    sw_module.fetch_entity_state_history_range = history_state_fetch
    try:
        return resolve_fn(
            day_start=DAY_START,
            day_end=DAY_END,
            grid_times=GRID_TIMES,
            period_hours=PERIOD_HOURS,
            n_periods=N_PERIODS,
        )
    finally:
        sw_module.NATIVE.hass = original_hass
        sw_module.fetch_entity_history_range = original_history
        sw_module.fetch_entity_power_history_kw = original_power
        sw_module.fetch_entity_state_history_range = original_state


# ---------------------------------------------------------------------------
# Serialization -- turns real BatteryConfig/ndarray/datetime output into
# plain JSON-safe data, so the SAME representation can be written by
# capture.py (reference revision) and compared against by the pytest test
# (current implementation) without either side needing to import the
# other's code.
# ---------------------------------------------------------------------------


def _json_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (frozenset, set)):
        return sorted(_json_safe(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in sorted(value.items())}
    if hasattr(value, "tolist"):  # numpy array / scalar
        return _json_safe(value.tolist())
    raise TypeError(f"don't know how to make {value!r} ({type(value)}) JSON-safe")


def serialize_battery_config(cfg) -> dict:
    """EVERY dataclass field of BatteryConfig, by name -- deliberately
    not a curated subset, so a field this fixture doesn't specifically
    exercise still shows up (and matches) rather than being silently
    excluded from the comparison. elements.py is unchanged by #1308 (see
    the PR's own diff), so the reference and current field sets are
    identical.

    One field is excluded, and only after asserting its value: nimbus #1417's
    `period0_pin_net_kw` is a diagnostic-only field added after the reference
    commit, so the reference cannot carry it. Neither function under
    characterization may ever set it -- it belongs on a diagnostic re-solve,
    never on a published solve's batteries -- so it must read None here, and a
    regression that set it would still fail this comparison."""
    out = {}
    for f in dataclasses.fields(cfg):
        value = getattr(cfg, f.name)
        if f.name in _ADDED_AFTER_REFERENCE:
            assert value is None, (
                f"{f.name} must never be set by the functions under "
                f"characterization, got {value!r} (nimbus #1417)"
            )
            continue
        out[f.name] = _json_safe(value)
    return out


#: BatteryConfig fields added after the reference commit 0d0d553a, which must
#: be None on everything these two functions build (see above).
_ADDED_AFTER_REFERENCE = frozenset({"period0_pin_net_kw"})


def serialize_build_extra_batteries(batteries: list) -> list:
    return [serialize_battery_config(b) for b in batteries]


def serialize_resolve_history(results: list) -> list:
    out = []
    for cfg, charge_kw, discharge_kw, final_soc, soc_hist in results:
        out.append(
            {
                "battery": serialize_battery_config(cfg),
                "actual_charge_kw": _json_safe(charge_kw),
                "actual_discharge_kw": _json_safe(discharge_kw),
                "final_soc_kwh_actual": _json_safe(final_soc),
                "soc_hist": [[t.isoformat(), float(v)] for t, v in soc_hist],
            }
        )
    return out


def capture(sw_module, build_extra_batteries_fn, resolve_history_fn) -> dict:
    """The single entry point both capture.py and the pytest test call --
    guarantees the two sides always run the identical scenario."""
    built = run_build_extra_batteries(sw_module, build_extra_batteries_fn)
    resolved = run_resolve_history(sw_module, resolve_history_fn)
    return {
        "build_extra_batteries": serialize_build_extra_batteries(built),
        "resolve_history": serialize_resolve_history(resolved),
    }
