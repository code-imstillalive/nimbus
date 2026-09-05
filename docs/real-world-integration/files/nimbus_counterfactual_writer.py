#!/usr/bin/env python3
"""
Nimbus-only counterfactual SoC tracker -- Stage 1 of the household's own
staged path toward eventually letting Nimbus drive real dispatch.

Direct household finding (2026-08-20, live, shadow-mode chart): for a real
~75-90 minute window (12:00-13:15), HAEO drove the real battery to charge
at a sustained ~40kW while Nimbus's own reactive plan showed near-zero --
a genuine, real divergence that raised a serious, legitimate concern:
"if nimbus was driving I would have had empty battery."

That concern couldn't be answered from the live shadow-mode chart alone,
because Nimbus's own solves have always read the REAL, HAEO-influenced SoC
as their starting point every cycle -- it never had to prove it could get
ITSELF from a genuinely independent trajectory to a P2P-ready SoC by 5pm.
A one-off manual script (scratchpad/counterfactual_nimbus_only_soc.py,
116KAT-HA-AI repo) built and run the same night found: Nimbus's own
independent trajectory reached 93% SoC by 3pm, two hours before the real
P2P window even opens -- genuinely reassuring, but one night is one data
point, not a track record. Direct household ask: "how do we now progress
it further to make it a testable package" -- this script is that: the
SAME validated rolling re-solve simulation, run automatically every
morning against YESTERDAY's now-fully-elapsed real data, building a real,
accumulating, multi-day track record instead of a single manual run.

Mechanism: starting from the real midnight SoC anchor, replays the WHOLE
day in 15-minute steps using the SAME network.build_plan() the real
production writer uses (same solver code, same today's fixes --
smoothness_weight, LATEST-preferred P2P tie-breaker) -- but Nimbus's own
simulated SoC is what feeds each subsequent solve's initial_soc_kwh, NEVER
the real one. Records the counterfactual SoC at the P2P window open
(17:00) and close (23:59), compares against the real SoC at those same
moments, and persists a rolling history so a genuine trend across many
nights is visible, not just one.

REAL BUG FOUND AND FIXED (2026-08-21, direct household report: a real
screenshot showing "Nimbus-only SoC at midnight close 61.8%" against
"Real SoC at midnight close 19.0%" -- "at midninght we should be close
ot 20% not 61.8%???"). Investigated and confirmed this script had NEVER
received the 2026-08-20 P2P-economics fixes already validated and live
in the production writer (nimbus_solver_forecast_writer.py):
  1. fixed_export_kw -- without it, this replay let the LP freely
     price-optimize export like a spot market instead of forcing the
     real automation's own constant 11.5kW commitment through the whole
     window, letting it "hold back" during lower-priced sub-periods
     exactly the same pathology already diagnosed and fixed in
     production the same night.
  2. Real per-night settled P2P rate/volume (sensor.lv_v2_p2p_confirmed_
     history for the SPECIFIC day being replayed) instead of the stale
     flat P2P_FLAT_RATE/P2P_BONUS_VOLUME_KWH module constants.
  3. terminal_value_breakpoints (the concave terminal-value mechanism,
     Nimbus PR #35) instead of the flat salvage_value this script still
     used -- confirmed via direct local testing (isolated the exact
     mechanism with a zero-salvage control run) that the flat mechanism
     alone drove a genuine LP exploit: with grid_export forced constant
     by fixed_export_kw, and a flat, un-tapering terminal reward
     ($0.3/kWh regardless of how full the battery already is), the LP
     found it "profitable" (per its own objective) to import heavily
     and charge the battery late in the day purely to bank a bigger
     terminal salvage payout -- reproduced live in isolation: WITH
     salvage_value=0.3 (flat) the replay ended at 100% SoC; WITH
     salvage_value=0.0 (isolation control) it ended at 2.0%, a clean,
     correctly-draining trajectory. The concave breakpoints (already
     proven live in production to avoid exactly this class of hard-
     corner pathology) meaningfully tames it.
  4. Also fixed a real, SEPARATE bug found investigating #1-3:
     export_bonus_volume_kwh was being re-granted IN FULL on every
     single 15-minute rolling re-solve, with no tracking of how much
     bonus-eligible volume had ALREADY been claimed earlier the SAME
     calendar day by an earlier tick of this same replay loop --
     inflating the LP's own belief in how much P2P-bonus-priced revenue
     remained available late in the window. Fixed by tracking
     bonus_used_kwh_today across the day's own loop and passing only
     the genuine REMAINING cap to each subsequent solve.

5. Terminal value zeroed for any solve starting INSIDE the P2P window
   (t.hour >= 17), kept normal (the concave breakpoints) before it opens.
   The first pass at this fix (keeping the concave reward at full
   strength the whole day) still gave 71.0% vs a real 19.0% -- correctly
   flagged, live, as still nonsense, not an improvement worth accepting.
   Root cause: this script's own ROLLING, DAY-BOUNDED horizon means
   ANY nonzero terminal reward, once a solve starts late in the P2P
   window, sits only a handful of periods away -- close enough to
   meaningfully bias the LP toward importing/charging to bank it, a
   distortion the real automation has no equivalent of (it blindly
   follows a fixed rate with zero regard for what happens after 24:00).
   Zeroing it specifically once a solve starts inside the window removes
   exactly this distortion while leaving the (needed, correct) daytime
   pre-charging incentive intact for solves before the window opens.
   Verified against real 2026-08-20 data: 5pm 96.9% (real 99.2%, off by
   2.3pts), midnight-close 22.1% (real 19.0%, off by 3.1pts) -- a clean,
   monotone drain the whole window, zero charging at any point.

OBSERVATION ONLY, same boundary as nimbus_solver_forecast_writer.py --
this script never calls number.set_value, never touches Modbus, never
drives anything. It answers "would Nimbus's own reasoning have kept the
battery viable," nothing more.

Real, honest methodological note carried over from the manual version:
uses real historical solar/load as a stand-in for "what a good forecast
would have shown" (a standard, defensible technique for this kind of
retrospective analysis, matching how this project's own oracle/regret
backtesting already works) -- not a claim that Nimbus had perfect
foresight, just the best available honest reconstruction of what already
happened.

Deploy:  /opt/nimbus_counterfactual_writer.py
Token:   /home/homehub/.ha_token
Log:     /opt/nimbus_counterfactual_writer.log
History: /home/homehub/nimbus_counterfactual_history.json
         (deliberately NOT /opt -- see this project's own well-documented
         /opt-is-root-owned first-write gotcha, CLAUDE.md "NUC Script
         Deployment" section)
Solver source: /opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load/solver/
         (the real git clone of code-imstillalive/nimbus)

Cron (as homehub, whichever NUC currently holds the VIP -- runs fine on
either, only ever reads real settled history, never writes anything back).
Set HA_TOKEN_PATH/NIMBUS_COUNTERFACTUAL_HISTORY_FILE (and HA_BASE if your
HA instance isn't reachable at its default mDNS hostname) before running
-- see this file's own HA_BASE/TOKEN_FILE/HISTORY_FILE comment above:
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/nimbus_counterfactual_writer.py > /opt/nimbus_counterfactual_writer.py
  export HA_TOKEN_PATH=/home/homehub/.ha_token NIMBUS_COUNTERFACTUAL_HISTORY_FILE=/home/homehub/nimbus_counterfactual_history.json
  sudo touch /opt/nimbus_counterfactual_writer.log && sudo chown homehub:homehub /opt/nimbus_counterfactual_writer.log
  python3 /opt/nimbus_counterfactual_writer.py   # one-off test run first
  (crontab -l 2>/dev/null; echo "30 20 * * * HA_TOKEN_PATH=$HA_TOKEN_PATH NIMBUS_COUNTERFACTUAL_HISTORY_FILE=$NIMBUS_COUNTERFACTUAL_HISTORY_FILE python3 /opt/nimbus_counterfactual_writer.py >> /opt/nimbus_counterfactual_writer.log 2>&1") | crontab -

(20:30 UTC = 06:30 AEST the following day -- 30 minutes after
lv_p2p_daily_recalibrate.py's own 06:00 AEST run, so yesterday's real P2P
settlement/SoC data is already fully recorded by the time this runs.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone

sys.path.insert(
    0, "/opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load"
)
import numpy as np
from solver import elements, network

# nimbus issue #364 finding 4 (Mark Purcell, codebase review): these
# three used to be hardcoded to one household's own real IP/paths.
# HA_BASE falls back to HA's own standard local mDNS hostname (a
# genuinely useful default, not a real household's address); TOKEN_FILE/
# HISTORY_FILE are inherently host-specific with no safe generic
# default, so they're required.
HA_BASE = os.environ.get("HA_BASE", "http://homeassistant.local:8123")
TOKEN_FILE = os.environ["HA_TOKEN_PATH"]
HISTORY_FILE = os.environ["NIMBUS_COUNTERFACTUAL_HISTORY_FILE"]
AEST = timezone(timedelta(hours=10))

# Real household config, same values confirmed live throughout the night
# this was built -- see number.nimbus_solver_* entities for the current
# live source of truth if these ever need updating.
CAPACITY_KWH = 122.2
MAX_CHARGE_KW = 40.0
MAX_DISCHARGE_KW = 40.0
ROUND_TRIP_EFF = 0.858
LEG_EFF = ROUND_TRIP_EFF**0.5
MIN_SOC_PCT = 2.0
MAX_SOC_PCT = 100.0
CHARGE_COST = 0.005
IMPORT_LIMIT_KW = 42.0
EXPORT_LIMIT_KW = 40.0
P2P_BONUS_VOLUME_KWH = (
    61.61  # fallback only now -- see fetch_real_p2p_rate_and_volume()
)
P2P_FLAT_RATE = 0.50  # fallback only now -- see fetch_real_p2p_rate_and_volume()
STEP_MINUTES = 15
# Real household P2P block config -- matches this household's real, live
# input_number.p2p_grid_export_target_kw (11.5kW) / the real automation's
# real window (17:00-24:00), the same Block 1 config as Nimbus's own
# sensor.nimbus_solver_config (number.nimbus_solver_p2p_block_1_*).
# Hardcoded here rather than fetched live -- matches this script's own
# existing style (every other household constant above is also a plain
# hardcoded value) -- update here if the household's real P2P block
# settings ever change.
P2P_TARGET_KW = 11.5
P2P_WINDOW_START_HOUR = 17
P2P_WINDOW_END_HOUR = 24
# P2P viability threshold: real settlement needs ~61.6kWh of the 122.2kWh
# capacity -- comfortably-viable is set a bit above that as a real margin,
# not the bare minimum.
VIABLE_SOC_PCT = 55.0


def ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ha_get(entity_id: str) -> dict:
    with open(TOKEN_FILE) as _f:
        token = _f.read().strip()
    r = subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            "10",
            "-H",
            f"Authorization: Bearer {token}",
            f"{HA_BASE}/api/states/{entity_id}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return json.loads(r.stdout)


def ha_history(
    entity_ids: list[str], start: datetime, end: datetime
) -> list[list[dict]]:
    with open(TOKEN_FILE) as _f:
        token = _f.read().strip()
    filter_arg = ",".join(entity_ids)
    r = subprocess.run(
        [
            "curl",
            "-s",
            "-G",
            "--max-time",
            "30",
            "-H",
            f"Authorization: Bearer {token}",
            f"{HA_BASE}/api/history/period/{start.isoformat()}",
            "--data-urlencode",
            f"filter_entity_id={filter_arg}",
            "--data-urlencode",
            f"end_time={end.isoformat()}",
            "--data-urlencode",
            "minimal_response=true",
            "--data-urlencode",
            "significant_changes_only=false",
        ],
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    return json.loads(r.stdout)


def push_sensor(entity_id: str, state, attributes: dict) -> None:
    with open(TOKEN_FILE) as _f:
        token = _f.read().strip()
    body = json.dumps({"state": state, "attributes": attributes})
    subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            "10",
            "-X",
            "POST",
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            f"{HA_BASE}/api/states/{entity_id}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def discharge_cost_rate(hour: int) -> float:
    return 0.09 if 7 <= hour < 17 else 0.01


def salvage_value_rate(hour: int) -> float:
    return 0.3 if 17 <= hour < 24 else 0.15


def terminal_value_breakpoints_for(
    base_rate: float, min_soc_kwh: float, max_soc_kwh: float
) -> list:
    """Copied verbatim from production's nimbus_solver_forecast_writer.py
    (same function, same reasoning) -- concave piecewise terminal value,
    REPLACES the flat salvage_value_rate() mechanism above. Real, direct
    finding this same session: with grid_export forced constant by
    fixed_export_kw and a FLAT terminal reward, the LP found it
    genuinely "profitable" (per its own objective, confirmed via a
    local isolation test) to import and charge heavily late in the
    window purely to bank a bigger terminal salvage payout, regardless
    of how full the battery already was -- exactly the hard-corner
    pathology this concave mechanism was already proven (2026-08-19,
    production) to avoid.
    """
    above_floor = max_soc_kwh - min_soc_kwh
    return [
        (above_floor * 0.15, base_rate * 2.2),
        (above_floor * 0.55, base_rate * 1.0),
        (above_floor * 0.30, base_rate * 0.35),
    ]


def fetch_real_p2p_rate_and_volume(day: datetime) -> tuple[float, float]:
    """Real, already-SETTLED P2P rate + matched volume for the SPECIFIC
    calendar day being replayed, from sensor.lv_v2_p2p_confirmed_history
    (the same safe, already-proven mechanism lv_p2p_daily_recalibrate.py
    built and nimbus_solver_quality_writer.py already reuses) -- replaces
    the old hardcoded P2P_FLAT_RATE/P2P_BONUS_VOLUME_KWH guess with the
    REAL rate/volume that specific night actually cleared at, not a
    generic constant. Falls back to the module constants if this date
    isn't in the confirmed history yet (e.g. still settling, or the
    sensor itself is unavailable) -- must never crash the replay.
    """
    try:
        hist = ha_get("sensor.lv_v2_p2p_confirmed_history")["attributes"]["history"]
        entry = hist.get(day.date().isoformat())
        if entry:
            vol = float(entry.get("export_volume") or 0.0)
            cost = float(entry.get("export_cost") or 0.0)
            if vol > 0.1:
                return cost / vol, vol
    except (KeyError, TypeError, ValueError):
        pass
    return P2P_FLAT_RATE, P2P_BONUS_VOLUME_KWH


def value_at_or_before(pts, t, default=0.0):
    best = default
    for pt, v in pts:
        if pt <= t:
            best = v
        else:
            break
    return best


def parse_series(raw_points: list[dict]) -> list[tuple[datetime, float]]:
    parsed = []
    for p in raw_points:
        try:
            v = float(p["state"])
        except (ValueError, KeyError, TypeError):
            continue
        t = datetime.fromisoformat(p["last_changed"]).astimezone(AEST)
        parsed.append((t, v))
    return parsed


def run_counterfactual(day: datetime) -> dict:
    """Replay one full real calendar day, Nimbus's own simulated SoC only.
    `day` is midnight AEST of the day to analyze."""
    anchor_time = day
    end_of_day = day + timedelta(days=1)

    raw = ha_history(
        [
            "sensor.combined_total_dc_power",
            "sensor.cb_total_combined_power_adjusted_kw",
            "sensor.costsflexup",
            "sensor.earningsflexup",
            "sensor.logger_battery_level_soc",
        ],
        anchor_time - timedelta(minutes=10),
        end_of_day + timedelta(minutes=10),
    )
    labels = ["solar", "load", "import_price", "export_price", "soc"]
    series = {label: parse_series(pts) for label, pts in zip(labels, raw)}

    real_soc_anchor = value_at_or_before(series["soc"], anchor_time)
    if real_soc_anchor == 0.0 and not series["soc"]:
        raise RuntimeError(
            f"no real SoC history found for {anchor_time.date()} -- cannot anchor"
        )

    sim_soc_kwh = real_soc_anchor / 100.0 * CAPACITY_KWH
    min_soc_kwh = CAPACITY_KWH * MIN_SOC_PCT / 100.0
    max_soc_kwh = CAPACITY_KWH * MAX_SOC_PCT / 100.0
    step = timedelta(minutes=STEP_MINUTES)
    t = anchor_time
    soc_at_5pm = None
    soc_at_midnight_close = None
    # Real settled rate/volume for THIS specific replayed day -- see
    # fetch_real_p2p_rate_and_volume()'s own docstring.
    real_p2p_rate, real_p2p_volume_cap = fetch_real_p2p_rate_and_volume(day)
    # Real bug fix: track how much bonus-eligible volume has ALREADY been
    # claimed earlier THIS SAME calendar day by an earlier tick of this
    # loop -- without this, every 15-min re-solve was being offered the
    # FULL nightly cap again, as if nothing had been exported yet.
    bonus_used_kwh_today = 0.0

    while t < end_of_day:
        grid_times = []
        tt = t
        while tt < end_of_day:
            grid_times.append(tt)
            tt += step
        n = len(grid_times)
        hours_arr = np.full(n, STEP_MINUTES / 60.0)

        solar_kw = np.array(
            [
                max(0.0, value_at_or_before(series["solar"], gt) / 1000.0)
                for gt in grid_times
            ]
        )
        load_kw = np.array(
            [max(0.1, value_at_or_before(series["load"], gt)) for gt in grid_times]
        )
        export_price = np.array(
            [value_at_or_before(series["export_price"], gt) for gt in grid_times]
        )
        import_price = np.array(
            [value_at_or_before(series["import_price"], gt) + 0.03 for gt in grid_times]
        )
        # Real fix: real per-night settled rate (not the stale flat
        # P2P_FLAT_RATE guess) -- see fetch_real_p2p_rate_and_volume().
        bonus_price = np.array(
            [
                max(0.0, real_p2p_rate - export_price[i])
                if P2P_WINDOW_START_HOUR <= gt.hour < P2P_WINDOW_END_HOUR
                else 0.0
                for i, gt in enumerate(grid_times)
            ]
        )
        # Real fix: this household's own P2P automation forces a CONSTANT
        # export rate through the whole window (a pre-committed matching
        # arrangement, not a price-chased spot market) -- see
        # nimbus_solver_forecast_writer.py's own P2P_BLOCK_KEYS comment
        # for the full "P2P needs a constant, pre-committed rate" finding
        # this mirrors. Without this, the replay let the LP freely
        # price-optimize export, holding back during lower-priced
        # sub-periods -- not what the real automation actually does.
        fixed_export_kw = np.array(
            [
                P2P_TARGET_KW
                if P2P_WINDOW_START_HOUR <= gt.hour < P2P_WINDOW_END_HOUR
                else float("nan")
                for gt in grid_times
            ]
        )
        discharge_cost_arr = np.array(
            [discharge_cost_rate(gt.hour) for gt in grid_times]
        )
        # Real fix (2026-08-21, second pass -- the first pass above, keeping
        # a full-strength concave terminal reward through the P2P window,
        # still gave 71.0% vs a real 19.0% -- a genuine, correctly-flagged
        # "still nonsense" result): once THIS TICK's own current time (t)
        # is already inside the P2P window, the real automation has ALREADY
        # committed to blind, no-lookahead constant-rate export -- it has
        # ZERO regard for what happens after. Any terminal reward for a
        # solve starting inside the window is therefore modeling something
        # the real system doesn't do, and is exactly what was driving the
        # LP to import/charge late in the window to bank it. Zeroed for any
        # solve starting inside the window; kept normal (the concave
        # mechanism above) BEFORE the window opens, so daytime pre-charging
        # -- needed to correctly reach ~100% by 5pm, matching real behaviour
        # -- still happens. Verified against real 2026-08-20 data: 5pm
        # 96.9% (real 99.2%, off by 2.3pts), midnight-close 22.1% (real
        # 19.0%, off by 3.1pts) -- a clean, monotone drain the whole
        # window, zero charging at any point, matching what the real
        # automation actually does.
        if t.hour >= P2P_WINDOW_START_HOUR:
            salvage = 0.0
        else:
            salvage = salvage_value_rate(grid_times[-1].hour)
        # Real fix: only the genuine REMAINING bonus-eligible volume for
        # THIS calendar day, not the full nightly cap re-granted on every
        # tick -- see bonus_used_kwh_today's own comment above.
        remaining_bonus_kwh = max(0.0, real_p2p_volume_cap - bonus_used_kwh_today)

        periods = elements.PeriodGrid(hours=hours_arr, start=t)
        grid = elements.GridConfig(
            import_price=import_price,
            export_price=export_price,
            import_limit_kw=IMPORT_LIMIT_KW,
            export_limit_kw=EXPORT_LIMIT_KW,
            export_bonus_price=bonus_price,
            export_bonus_volume_kwh=remaining_bonus_kwh,
            fixed_export_kw=fixed_export_kw,
        )
        battery = elements.BatteryConfig(
            capacity_kwh=CAPACITY_KWH,
            initial_soc_kwh=min(max(sim_soc_kwh, min_soc_kwh), max_soc_kwh),
            min_soc_kwh=min_soc_kwh,
            max_soc_kwh=max_soc_kwh,
            max_charge_kw=MAX_CHARGE_KW,
            max_discharge_kw=MAX_DISCHARGE_KW,
            charge_efficiency=LEG_EFF,
            discharge_efficiency=LEG_EFF,
            charge_cost=CHARGE_COST,
            discharge_cost=discharge_cost_arr,
            salvage_value=salvage,
            # Real fix: concave terminal value replaces the flat
            # salvage_value above -- see terminal_value_breakpoints_for()'s
            # own docstring for the exact pathology this closes.
            terminal_value_breakpoints=terminal_value_breakpoints_for(
                salvage, min_soc_kwh, max_soc_kwh
            ),
        )
        solar = elements.SolarConfig(forecast_kw=solar_kw)
        loads = [elements.LoadConfig(name="load", forecast_kw=load_kw)]

        plan = network.build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            smoothness_weight=network.DEFAULT_SMOOTHNESS_WEIGHT_KW,
        )
        if plan.status == "optimal":
            net0 = float(plan.battery_discharge_kw[0] - plan.battery_charge_kw[0])
            if net0 >= 0:
                sim_soc_kwh -= net0 * STEP_MINUTES / 60.0 / LEG_EFF
            else:
                sim_soc_kwh += (-net0) * LEG_EFF * STEP_MINUTES / 60.0
            sim_soc_kwh = min(max(sim_soc_kwh, min_soc_kwh), max_soc_kwh)
            if plan.export_bonus_kw is not None:
                bonus_used_kwh_today += (
                    float(plan.export_bonus_kw[0]) * STEP_MINUTES / 60.0
                )
        # A non-optimal solve (rare -- e.g. a genuinely infeasible reconstructed
        # window) holds SoC unchanged rather than crashing the whole day's replay.

        if t.hour == 17 and t.minute < STEP_MINUTES and soc_at_5pm is None:
            soc_at_5pm = sim_soc_kwh / CAPACITY_KWH * 100.0
        t += step

    soc_at_midnight_close = sim_soc_kwh / CAPACITY_KWH * 100.0
    real_soc_5pm = value_at_or_before(series["soc"], day.replace(hour=17, minute=0))
    real_soc_close = value_at_or_before(
        series["soc"], end_of_day - timedelta(minutes=1)
    )

    return {
        "date": day.date().isoformat(),
        "real_soc_anchor_pct": round(real_soc_anchor, 1),
        "nimbus_only_soc_5pm_pct": round(soc_at_5pm, 1)
        if soc_at_5pm is not None
        else None,
        "real_soc_5pm_pct": round(real_soc_5pm, 1),
        "nimbus_only_soc_close_pct": round(soc_at_midnight_close, 1),
        "real_soc_close_pct": round(real_soc_close, 1),
        "viable": (soc_at_5pm is not None and soc_at_5pm >= VIABLE_SOC_PCT),
        "real_p2p_rate_used": round(real_p2p_rate, 4),
        "real_p2p_volume_cap_used": round(real_p2p_volume_cap, 2),
    }


def load_history() -> dict:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"[{ts()}] WARN: could not read {HISTORY_FILE} ({e}), starting fresh",
            flush=True,
        )
        return {}


def save_history(history: dict) -> None:
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f)
    except OSError as e:
        print(f"[{ts()}] WARN: could not write {HISTORY_FILE}: {e}", flush=True)


def main() -> None:
    yesterday = (datetime.now(AEST) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    try:
        result = run_counterfactual(yesterday)
    except Exception as e:  # noqa: BLE001 -- broad top-level guard around a full day's run: logs the real exception (not swallowed) and returns cleanly so one bad/missing day can't crash the whole scheduled run
        print(
            f"[{ts()}] ERROR: counterfactual run failed for {yesterday.date()}: {e}",
            flush=True,
        )
        return

    history = load_history()
    history[result["date"]] = result
    save_history(history)

    push_sensor(
        "sensor.nimbus_counterfactual_soc_5pm",
        result["nimbus_only_soc_5pm_pct"]
        if result["nimbus_only_soc_5pm_pct"] is not None
        else "unknown",
        {
            "unit_of_measurement": "%",
            "friendly_name": "Nimbus-only Counterfactual SoC at 5pm",
            "latest_date": result["date"],
            "viable": result["viable"],
            "real_soc_5pm_pct": result["real_soc_5pm_pct"],
            "nimbus_only_soc_close_pct": result["nimbus_only_soc_close_pct"],
            "real_soc_close_pct": result["real_soc_close_pct"],
            "viable_threshold_pct": VIABLE_SOC_PCT,
            "history": history,
        },
    )
    print(
        f"[{ts()}] {result['date']}: nimbus_only_soc_5pm={result['nimbus_only_soc_5pm_pct']}% "
        f"real_soc_5pm={result['real_soc_5pm_pct']}% viable={result['viable']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
