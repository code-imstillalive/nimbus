#!/usr/bin/env python3
"""Runs once daily (yesterday's real, fully-settled data only) and
answers the household's own direct, explicit request (2026-08-17): "I
think we should have a live tracker of the regret value and EPR score
on the screen as we keep going through the solver so we know if it is
doing better."

Pulls the same three inputs quality_report.py's own module docstring
describes -- a real, hypothetical PERFECT-FORESIGHT oracle re-solved
against yesterday's real, ALREADY-KNOWN solar/load/price data (never a
forecast -- forecasting error has NOTHING to do with this score, only
plan/dispatch QUALITY does, per regret.py's own module docstring
distinction), yesterday's real MEASURED battery dispatch, and
yesterday's real SETTLED P2P dollars (sensor.lv_v2_p2p_confirmed_
history, the same safe, zero-recorder-risk mechanism this project's own
lv_p2p_daily_recalibrate.py already proved out) -- then pushes ONE
number.epr + a real, growing per-day HISTORY trend (mirroring
sensor.lv_v2_p2p_confirmed_history's own {date: {...}} shape exactly) to
sensor.nimbus_solver_quality_report, so a dashboard chart can show
whether the Solver is genuinely getting better night over night, not
just a single day's snapshot.

OBSERVATION ONLY -- same standing guarantee as nimbus_solver_forecast_
writer.py: one GET sweep, one POST to a plain sensor, nothing else. This
script does not exist anywhere near the real, live battery dispatch
path (that's exclusively automations.yaml's own
p2p_battery_sell_5pm_midnight / haeo_battery_automation_original).

Real, honest design choice worth stating: this deliberately runs against
"the most recently fully-elapsed real calendar day" (yesterday), NOT
today -- P2P settlement genuinely only becomes real, trustworthy ground
truth overnight (LocalVolts' own Exp->Act batch, same real mechanism
lv_p2p_daily_recalibrate.py was built around), so a "today so far" score
would necessarily be scored against still-provisional numbers. Real
inspiration for the whole run-once-daily-for-yesterday shape:
lv_p2p_daily_recalibrate.py's own proven pattern.

Deploy (run via cron on whichever NUC currently holds the VIP -- runs on
the NUC HOST, not inside the HA container, same as every writer in this
project):
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/nimbus_solver_quality_writer.py > /opt/nimbus_solver_quality_writer.py
  # 2026-08-17: same new dependency as nimbus_solver_forecast_writer.py's
  # own deploy docstring -- config/integrations/nimbus_solver_battery_
  # config.yaml's new input_number helpers need a full HA restart before
  # this script's ha_get() calls against them will succeed.
  # /opt is root-owned -- pre-create the log file first, or cron's own
  # `>>` redirect fails silently forever and the script never runs at
  # all. Confirmed live 2026-08-17 (same bug hit nimbus_solver_forecast_
  # writer.py's own log the same day) -- see that script's own deploy
  # docstring for the full incident.
  sudo touch /opt/nimbus_solver_quality_writer.log && sudo chown homehub:homehub /opt/nimbus_solver_quality_writer.log
  python3 /opt/nimbus_solver_quality_writer.py   # one-off test run first
  (crontab -l 2>/dev/null; echo "* * * * * python3 /opt/nimbus_solver_quality_writer.py >> /opt/nimbus_solver_quality_writer.log 2>&1") | crontab -
  # 2026-08-17, direct real ask: "we want to be better not behind" (vs
  # HAEO's own faster cadence) -- was */5, before that a once-daily
  # 06:17 cron. Genuinely safe at 1-minute resolution and needs no
  # overlap guard (unlike nimbus_solver_forecast_writer.py's own real
  # 45-52s LP solve): the real LP solve here only ever happens ONCE per
  # new day (see the "already scored" early-exit below) -- every other
  # run this same day is just a cheap re-push of the cached result
  # (a handful of network round-trips, well under a second), which is
  # now exactly what keeps sensor.nimbus_solver_quality_report from
  # staying missing for any real length of time after a restart wipes
  # it (confirmed live, recurring multiple times the same session: a
  # REST-pushed sensor has no persistent HA backing). A day still not
  # yet present in sensor.lv_v2_p2p_confirmed_history simply skips
  # cleanly and retries on the next 1-min tick -- no explicit stagger
  # against lv_p2p_daily_recalibrate.py's own 06:00 run needed, a missed
  # early tick just retries a minute later.

Token: /home/homehub/.ha_token (same file every other writer script uses;
override via HA_TOKEN_PATH, override HA_BASE if HA isn't reachable at
localhost:8123 from wherever this runs, override NIMBUS_SOLVER_PATH if
your own solver/ clone lives somewhere other than this exact NUC path --
see nimbus_solver_forecast_writer.py's own equivalent comment for why).
State file (per-day rolling quality history, same /opt-root-owned
gotcha as every other new file in this project -- pre-`sudo touch` +
`chown` on first deploy): /opt/nimbus_solver_quality_history.json
Solver source: /opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load/solver/
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

# Same real, confirmed-live fix as nimbus_solver_forecast_writer.py's own
# top-of-file comment -- never trust the running environment's own
# system-local timezone resolution.
BRISBANE_TZ = ZoneInfo("Australia/Brisbane")

# Env-var-overridable (2026-09-05, nimbus issue #364 finding 4, same
# "installable by anyone" reasoning already applied to
# nimbus_solver_forecast_writer.py's own HA_BASE/TOKEN_PATH/
# NIMBUS_SOLVER_PATH on 2026-08-22) -- this file previously hardcoded all
# three to this one reference household's own NUC path/hostname/username
# with zero indirection at all, unlike its sibling writer script. The
# defaults below are kept identical to what this household's own NUC
# already runs, so this is a pure portability fix, zero behavior change
# for the existing deployment.
sys.path.insert(
    0,
    os.environ.get(
        "NIMBUS_SOLVER_PATH",
        "/opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load",
    ),
)
import numpy as np
from solver import elements
from solver.quality_report import compute_quality_report
from solver.tracking import compute_tracking_fidelity, tracking_error_cost

HA_BASE = os.environ.get("HA_BASE", "http://localhost:8123")
# ^ "localhost" only works if this script runs on the same machine as HA
# itself. Set the HA_BASE env var to HA's real LAN IP/hostname otherwise
# (e.g. "http://192.168.1.50:8123") -- see nimbus_solver_forecast_writer.py's
# own HA_BASE comment for the full reasoning, unchanged here.
TOKEN_PATH = os.environ.get("HA_TOKEN_PATH", "/home/homehub/.ha_token")
ENTITY_ID = "sensor.nimbus_solver_quality_report"
QUALITY_HISTORY_PATH = os.environ.get(
    "NIMBUS_SOLVER_QUALITY_HISTORY_PATH", "/opt/nimbus_solver_quality_history.json"
)

# 15-min resolution across a real 24h day -> 96 periods. Genuinely was
# NOT fine enough to catch the real inv1/inv2 handoff-style dips this
# project has documented extensively (roughly hourly, ~20-30s each) --
# confirmed live 2026-08-18: this grid's own tracking_fidelity/
# tracking_cost figures were still real, still a valid oracle-comparison
# input for the LP-solve-dependent EPR/regret computation (where this
# coarseness is a genuine, necessary LP-solve-cost tradeoff -- see
# nimbus_solver_forecast_writer.py's own measured cost curve, 96
# periods ~4.4s), but a 15-min "nearest-at-or-before" sample has only a
# small chance of ever landing during a real ~20-30s dip, systematically
# UNDER-counting the true tracking gap for that specific purpose.
# FINE_PERIOD_HOURS below is the fix -- compute_tracking_fidelity()/
# tracking_error_cost() (solver.tracking, item #6 of Mark Purcell's
# audit) have ZERO LP-solve dependency, pure array math against already-
# fetched history, so there's no reason for them to inherit this
# tradeoff at all.
PERIOD_HOURS = 0.25
N_PERIODS = 96

# 1-min resolution -> 1440 periods/day, used ONLY for the tracking_
# fidelity/tracking_cost computation below (see comment above) -- pure
# resampling of already-fetched history, not a second LP solve, so the
# real cost here is trivial regardless of grid size.
FINE_PERIOD_HOURS = 1.0 / 60.0
N_FINE_PERIODS = 1440

# Same real, bill-confirmed Energex NTC 6900 TOU schedule and
# certificates rate nimbus_solver_forecast_writer.py already uses --
# duplicated here (not imported) since this is a standalone script, same
# convention as every other writer in this project.
NETWORK_ENERGY_PEAK_RATE = 0.214863
NETWORK_ENERGY_OFFPEAK_RATE = 0.00476
NETWORK_ENERGY_SHOULDER_RATE = 0.066759
CERTIFICATES_RATE = 0.008246

# Same real per-window battery cost schedule (config/automations.yaml,
# "HAEO Battery Cost Schedule - 5pm/Midnight/7am") the forecast writer
# already uses.
BATTERY_DISCHARGE_COST_NIGHT = 0.01
BATTERY_DISCHARGE_COST_DAY = 0.09
# BATTERY_SALVAGE_VALUE_NIGHT/OTHER removed (2026-08-30) -- this scorer's own
# battery_cfg now always uses salvage_value=0.0, see that construction's own
# comment for the real, verified reason.

# Real, confirmed live bug fix (2026-08-18): sensor.logger_charging_
# discharging_command's own raw state is the numeric Modbus command CODE
# as a string ('170'/'187'/'204'), confirmed live via a direct state
# check -- NOT the word "Charge"/"Discharge"/"Stop" (that's a DIFFERENT
# entity, select.set_logger_charging_discharging_command, the UI-facing
# write-target). Comparing this sensor's state against the literal word
# strings (as this file did until now) never matches anything, ever --
# commanded_net_kw silently evaluated to 0.0 for every single period,
# every single day, since this feature was first built. This made
# tracking_fidelity hit the "nothing commanded" zero-guard (hardcoded
# 1.0) on every run regardless of real dispatch, while the OTHER
# tracking figures (mean_absolute_error_kw, worst_gap_kw, energy_
# shortfall_kwh) were silently measuring |achieved - 0| all along --
# i.e. just raw battery activity, not any real tracking deviation.
# Codes per this project's own documented register map (116KAT-HA-AI's
# own CLAUDE.md, "Sungrow register conventions"): 0xAA=170=Charge,
# 0xBB=187=Discharge, 0xCC=204=Stop. Deliberately still uses this raw
# Modbus-readback sensor (not the select) -- it's the closer-to-ground-
# truth signal (what the inverter actually confirmed, not just what was
# last written), which matters specifically for a feature whose whole
# point is catching real execution-layer discrepancies.
CMD_CODE_CHARGE = "170"
CMD_CODE_DISCHARGE = "187"
CMD_CODE_STOP_DEFAULT = "204"  # safe "nothing commanded" fallback, same intent as this file's old default="Stop"

# 2026-08-20 -- REPLACED the flat $0.50 placeholder this script used to
# share with HAEO's own Export price config (PR #308, sibling repo).
# Direct household finding: that flat rate is an arbitrary signal
# engineered specifically so HAEO's own LP would plan dispatch timing
# correctly -- it was never the real price, which genuinely varies
# 0-70c. This script only ever scores an already-elapsed day ("yesterday"
# -- see module docstring), so unlike the live forward-planning writer
# (nimbus_solver_forecast_writer.py's own resample_real_p2p_rate(), same
# day, same real fix) there's no forward-reliability question at all
# here: by the time this runs, that day's real per-interval rate is
# fully SETTLED (quality='Act'), the same ground truth sensor.lv_v2_p2p_
# confirmed_history's own daily total is built from. Fetches directly
# from LocalVolts' own v2 API (same endpoint/auth/curl-subprocess
# pattern as lv_p2p_forecast_writer.py's own fetch_intervals() -- see
# that script for why subprocess+curl, not urllib: a documented, real,
# silent-failure difference between the two on this specific API).
LV_API = "https://api2.localvolts.com/v2/customer/interval"
SECRETS_FILE = "/opt/homeassistant/config/secrets.yaml"


def get_secret(key: str) -> str:
    with open(SECRETS_FILE, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(key + ":"):
                return line.split('"')[1]
    raise SystemExit(f"{key} not found in {SECRETS_FILE}")


def fetch_real_p2p_rates_for_day(
    target_date, grid_times: list[datetime]
) -> list[float] | None:
    """Real, per-interval SETTLED P2P export rate ($/kWh) for one specific,
    already-elapsed calendar day -- the retrospective sibling of
    nimbus_solver_forecast_writer.py's resample_real_p2p_rate() (same
    project, same day, same underlying finding). Rate formula identical
    to that function and to this project's own "P2P Trades Tonight" card:
        rate = matchedCost / (volume * proportionP2P)

    Returns None (not a flat fallback) on any real failure -- credentials
    missing, API error, zero real Sell/Act records for the day -- so
    main() can skip this day and retry next run, the SAME established
    pattern this file already uses when sensor.lv_v2_p2p_confirmed_
    history doesn't have the day yet. Deliberately does NOT fall back to
    the old flat placeholder on failure: since this score's entire
    purpose is measuring real economic quality, silently reverting to a
    known-wrong flat rate would produce a misleading EPR, worse than
    honestly having no number yet for that day.
    """
    try:
        key = get_secret("localvolts_v2_api_key_header")
        partner = get_secret("localvolts_v2_partner")
    except SystemExit as e:
        print(
            f"could not read LocalVolts credentials ({e}) -- skipping P2P rate fetch",
            file=sys.stderr,
        )
        return None

    frm = (target_date - timedelta(days=1)).isoformat()
    to = (target_date + timedelta(days=1)).isoformat()
    url = f"{LV_API}?NMI=*&from={frm}&to={to}"
    try:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "-H",
                f"Authorization: {key}",
                "-H",
                f"partner: {partner}",
                "-H",
                "User-Agent: Home Assistant",
                url,
            ],
            # check=True: the except clause right below already explicitly
            # names subprocess.SubprocessError -- without check=True that
            # branch was dead code, a failed curl would silently succeed
            # with empty/garbage stdout instead of being caught here.
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        raw = json.loads(r.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        print(
            f"LocalVolts API fetch/parse failed ({e}) -- skipping P2P rate fetch",
            file=sys.stderr,
        )
        return None
    if isinstance(raw, dict) and raw.get("error"):
        print(
            f"LocalVolts API error: {raw} -- skipping P2P rate fetch", file=sys.stderr
        )
        return None

    pts = []
    for p in raw:
        try:
            if p.get("direction") != "Sell" or p.get("quality") != "Act":
                continue
            # REAL BUG FOUND AND FIXED (2026-08-21, live report: "not yet
            # available" for EPR, well past when settlement normally
            # completes): this used p["time"], a field that doesn't exist
            # on the raw v2 API's own records at all (confirmed against
            # lv_p2p_forecast_writer.py's own already-working use of
            # item.get('intervalEnd') for the exact same raw response
            # shape) -- every single record hit a KeyError here, silently
            # caught by this same try/except's own broad clause below, so
            # `pts` stayed empty EVERY run regardless of whether real
            # settled data existed. sensor.lv_v2_p2p_confirmed_history
            # (a separate, correctly-field-named mechanism) had the real
            # 2026-08-20 data the whole time -- this function's own
            # independent fetch just never actually found it.
            end_t = datetime.fromisoformat(p["intervalEnd"]).astimezone(BRISBANE_TZ)
            start_t = end_t - timedelta(minutes=5)
            vol = float(p.get("volume") or 0.0)
            prop = float(p.get("proportionP2P") or 0.0)
            cost = float(p.get("matchedCost") or 0.0)
            matched_vol = vol * prop
            rate = (cost / matched_vol) if matched_vol > 0.01 else 0.0
            pts.append((start_t, rate))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    if not pts:
        print(
            f"no real settled Sell/Act P2P records found for {target_date.isoformat()} -- skipping",
            file=sys.stderr,
        )
        return None

    out = []
    for gt in grid_times:
        if not (17 <= gt.hour < 24):
            out.append(0.0)
            continue
        val = pts[0][1]
        for t, v in pts:
            if t <= gt:
                val = v
            else:
                break
        out.append(float(val))
    return out


with open(TOKEN_PATH, "r", encoding="utf-8") as f:
    TOKEN = f.read().strip()


def ha_get(entity_id: str) -> dict:
    req = urllib.request.Request(
        f"{HA_BASE}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def ha_post_state(entity_id: str, state, attributes: dict) -> None:
    body = json.dumps({"state": state, "attributes": attributes}).encode("utf-8")
    req = urllib.request.Request(
        f"{HA_BASE}/api/states/{entity_id}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def num(entity_id: str) -> float:
    return float(ha_get(entity_id)["state"])


def network_energy_rate(hour: int) -> float:
    if 16 <= hour < 21:
        return NETWORK_ENERGY_PEAK_RATE
    if 11 <= hour < 16:
        return NETWORK_ENERGY_OFFPEAK_RATE
    return NETWORK_ENERGY_SHOULDER_RATE


def battery_discharge_cost_rate(hour: int) -> float:
    return (
        BATTERY_DISCHARGE_COST_NIGHT
        if (hour >= 17 or hour < 7)
        else BATTERY_DISCHARGE_COST_DAY
    )


# Real, live-reported bug (2026-08-29/30, issue tracked in 116KAT-HA-AI's own
# CLAUDE.md "invalid EPR (>100%, negative regret)" incident): this file's own
# battery_cfg used to credit leftover end-of-day SoC via a flat
# salvage_value*final_soc_kwh terminal-value term. On a day where the real
# dispatch accidentally ended near-full (e.g. a disrupted P2P sell automation
# barely discharging that night), that flat credit massively over-rewarded
# the accidental full ending relative to what even a fully unconstrained
# perfect-foresight oracle could match -- the oracle, scored the same way,
# correctly prefers SELLING energy during the day over holding it for a flat
# rate exceeding real achievable prices, so it can never "beat" a trajectory
# that got lucky on this technicality. This let real-achieved beat the oracle
# at spot-only economics -- structurally impossible, and exactly what
# produced EPR>100%/negative regret.
#
# A concave piecewise-linear terminal-value curve (same shape
# solver_writer.py's own live forward-planning path uses) was tried and
# measurably helped, but did NOT fully close the gap: ANY positive per-kWh
# credit for leftover battery energy, curved or flat, still rewards an
# accidental under-delivery, since the real trajectory ends full precisely
# BECAUSE it failed to deliver its committed export that night, while the
# oracle (correctly forced to honour the same real commitment) necessarily
# ends with less energy left over.
#
# The real, structural fix: this script evaluates exactly ONE already-elapsed
# calendar day in isolation. Crediting energy still in the battery at
# day-close is a guess about tomorrow's value this script has no honest basis
# for making -- tomorrow's own quality report, run independently against
# tomorrow's real initial_soc_kwh, is what actually prices whatever gets
# carried forward. Fixed by setting salvage_value=0.0 (no terminal value
# credit at all) rather than trying a better-shaped credit -- restores the
# one invariant EPR<=100%/regret>=0 structurally depend on: the oracle,
# optimizing the identical objective over the identical feasible region, can
# never be beaten by any other trajectory scored the same way.
#
# Verified against a real incident day, three approaches in order: flat
# salvage_value (145.0% EPR, -$18.15 regret -- both invalid) -> concave curve
# (127.7% EPR, -$11.14 regret -- still invalid) -> salvage_value=0.0 (76.0%
# EPR, +$8.94 regret -- both valid).


def fetch_history_range(
    entity_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, str]]:
    """Real recorded history for a single entity's raw state string, as
    (local BRISBANE_TZ time, raw state) points, for an EXPLICIT
    [start, end) window -- unlike nimbus_solver_forecast_writer.py's own
    fetch_price_history() (which always means "the last N days up to
    right now"), this script needs a specific, already-elapsed real
    calendar day, so start/end are both explicit real arguments here.
    Returns [] (callers fall back further) if genuinely unavailable --
    must never crash the writer.
    """
    url = (
        f"{HA_BASE}/api/history/period/{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    if not data or not data[0]:
        return []
    out: list[tuple[datetime, str]] = []
    for p in data[0]:
        state = p.get("state")
        if state in (None, "unknown", "unavailable"):
            continue
        out.append((parse_iso(p["last_changed"]).astimezone(BRISBANE_TZ), state))
    return sorted(out, key=lambda x: x[0])


def resample_nearest_float(
    pts: list[tuple[datetime, str]], grid_times: list[datetime], default: float = 0.0
) -> list[float]:
    """Nearest-at-or-before lookup against a real, explicit-window
    history fetch, parsed to float -- same convention as
    nimbus_solver_forecast_writer.py's own resample_forecast(), just
    against real recorded history points instead of a forecast array.
    Individual points that fail to parse as a float are silently
    skipped (a transient non-numeric state, e.g. "unknown" mid-stream --
    already filtered by fetch_history_range, but defensive here too).
    """
    numeric: list[tuple[datetime, float]] = []
    for t, s in pts:
        try:
            numeric.append((t, float(s)))
        except ValueError:
            continue
    out = []
    for gt in grid_times:
        val = numeric[0][1] if numeric else default
        for t, v in numeric:
            if t <= gt:
                val = v
            else:
                break
        out.append(val)
    return out


def resample_nearest_str(
    pts: list[tuple[datetime, str]], grid_times: list[datetime], default: str = ""
) -> list[str]:
    out = []
    for gt in grid_times:
        val = pts[0][1] if pts else default
        for t, v in pts:
            if t <= gt:
                val = v
            else:
                break
        out.append(val)
    return out


def value_at_or_before(
    pts: list[tuple[datetime, str]], t: datetime, default: float
) -> float:
    """Single-point nearest-before lookup (not a whole grid) -- used for
    the battery's own real start-of-day / end-of-day SoC%, which only
    needs two real values, not a full 96-point resample."""
    val = default
    for pt_t, pt_v in pts:
        if pt_t <= t:
            try:
                val = float(pt_v)
            except ValueError:
                continue
        else:
            break
    return val


def robust_value_near(
    pts: list[tuple[datetime, str]],
    t: datetime,
    window_seconds: float = 300.0,
    default: float | None = None,
) -> float:
    """Time-weighted "what was really true" lookup, for a value that must
    hold steady across a window (e.g. a real committed P2P target) but
    whose own recorder history can carry a real, brief, self-correcting
    transient landing exactly on the lookup instant.

    Found live 2026-08-29 (household reference deployment): a plain
    point-in-time lookup at exactly the P2P window's own start picked up
    a genuine few-second glitch in the target `input_number` -- see this
    project's own CLAUDE.md (116KAT-HA-AI repo) for the full incident.
    That forced the oracle's fixed export to the glitch value for the
    ENTIRE window, capping its forced-export volume at a small fraction
    of what was really delivered and invalidating that day's EPR/regret.

    Instead of trusting the single value in force at `t`, this walks
    every value change across [t, t + window_seconds) and returns
    whichever value held for the GREATEST total duration in that
    window -- a few-second blip can only ever contribute its own few
    seconds to the total, so it can't win against a genuinely-settled
    value, unless the signal itself is truly unstable (a real, different
    problem this function correctly can't paper over).

    Falls back to the plain at-or-before value if there's no history
    inside the window at all (e.g. very sparse recorder data), and to
    `default` if there's no history before `t` either -- same fallback
    contract as `value_at_or_before`.
    """
    baseline = default
    idx_after = 0
    for i, (pt_t, pt_v) in enumerate(pts):
        if pt_t <= t:
            try:
                baseline = float(pt_v)
            except ValueError:
                pass
            idx_after = i + 1
        else:
            break

    window_end = t + timedelta(seconds=window_seconds)
    totals: dict[float, float] = {}
    cur_val = baseline
    cur_start = t
    for pt_t, pt_v in pts[idx_after:]:
        if pt_t >= window_end:
            break
        try:
            v = float(pt_v)
        except ValueError:
            continue
        if cur_val is not None:
            totals[cur_val] = (
                totals.get(cur_val, 0.0) + (pt_t - cur_start).total_seconds()
            )
        cur_val, cur_start = v, pt_t
    if cur_val is not None:
        totals[cur_val] = (
            totals.get(cur_val, 0.0) + (window_end - cur_start).total_seconds()
        )

    if not totals:
        return baseline if baseline is not None else default
    return max(totals, key=totals.get)


def load_quality_history() -> dict:
    try:
        with open(QUALITY_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_quality_history(history: dict) -> None:
    try:
        with open(QUALITY_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except OSError as e:
        print(f"WARN: could not save quality history ({e})", file=sys.stderr)


def main() -> None:
    now = datetime.now(UTC).astimezone(BRISBANE_TZ)
    yesterday = (now - timedelta(days=1)).date()
    day_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, 0, 0, 0, tzinfo=BRISBANE_TZ
    )
    day_end = day_start + timedelta(days=1)
    day_key = yesterday.isoformat()

    # Real bug found live (2026-08-17): this used to just print "already
    # scored -- nothing to do" and return, based ONLY on whether today's
    # date already exists in the LOCAL, ON-DISK quality_history file --
    # which is real, persistent state that survives a restart. But
    # sensor.nimbus_solver_quality_report itself has NO persistent HA
    # backing (a plain REST-pushed sensor, same as every other computed
    # Solver sensor in this project) -- a full HA restart wipes the LIVE
    # sensor while leaving the on-disk history completely untouched,
    # and this early-exit skipped re-pushing it, so the dashboard card
    # was left showing its "no scored day yet" fallback indefinitely
    # until the NEXT real new day's cron run, even though the real,
    # already-computed answer was sitting right there on disk the whole
    # time. Same class of gap this project has already hit and fixed
    # for other REST-pushed sensors (log_monitor.py's own restore-if-
    # missing pattern).
    #
    # Fixed: "already scored" now only skips the EXPENSIVE recompute
    # (the real LP solve against yesterday's data) -- it still always
    # re-pushes the sensor from the cached data, cheap and safe to do on
    # every single run regardless. hourly_regret_latest_day is the one
    # real, minor thing this fast path can't include (only ever computed
    # fresh by compute_quality_report(), never persisted to the on-disk
    # history) -- an acceptable, honest gap: the headline EPR/regret/
    # tracking figures are all still exactly correct on this path, only
    # the hourly-breakdown chart detail is unavailable until the next
    # real new-day run recomputes it fresh.
    quality_history = load_quality_history()
    if day_key in quality_history:
        day_entry = quality_history[day_key]
        print(
            f"[{now.isoformat()}] {day_key} already scored (epr={day_entry.get('epr')}) -- re-pushing sensor (may have been wiped by a restart)"
        )
        ha_post_state(
            ENTITY_ID,
            day_entry["epr"],
            {
                "unit_of_measurement": None,
                "friendly_name": "Nimbus Solver Quality Report (EPR)",
                "latest_date": day_key,
                "history": quality_history,
                "generated_at": now.isoformat(),
                **day_entry,
            },
        )
        return

    # Real settled P2P ground truth (see module docstring) -- the whole
    # reason this runs a day BEHIND, not for today.
    try:
        confirmed_hist = ha_get("sensor.lv_v2_p2p_confirmed_history")["attributes"][
            "history"
        ]
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError) as e:
        print(
            f"[{now.isoformat()}] could not read confirmed P2P history ({e}) -- skipping, will retry next run",
            file=sys.stderr,
        )
        return
    day_data = confirmed_hist.get(day_key)
    if not day_data:
        print(
            f"[{now.isoformat()}] {day_key} not yet present in sensor.lv_v2_p2p_confirmed_history -- skipping, will retry next run"
        )
        return
    real_p2p_dollars = float(day_data.get("export_cost", 0.0))
    real_p2p_volume_kwh = float(day_data.get("export_volume", 0.0))

    grid_times = [
        day_start + timedelta(hours=i * PERIOD_HOURS) for i in range(N_PERIODS)
    ]
    period_hours_arr = [PERIOD_HOURS] * N_PERIODS

    # Real measured yesterday, not a forecast -- see module docstring for
    # why this must be real recorded history, never sensor.nimbus_*_
    # forecast (this score has nothing to do with forecast accuracy).
    solar_hist = fetch_history_range(
        "sensor.combined_total_dc_power", day_start, day_end
    )
    # Same real, cleaner whole-house load signal this project switched
    # both the live P2P automation AND Nimbus's own Whole House power
    # signal to (2026-08-16, see the sibling repo's own CLAUDE.md session
    # "Real P2P-window grid spikes root-caused...") -- NOT the noisy raw
    # sensor.logger_load_power this same investigation moved away from.
    load_hist = fetch_history_range(
        "sensor.cb_total_combined_power_adjusted_kw", day_start, day_end
    )
    # Real, signed net battery power (positive=discharge, this project's
    # own established convention) -- the ACTUAL trajectory.
    battery_actual_hist = fetch_history_range(
        "sensor.logger_battery_power", day_start, day_end
    )
    # Real COMMANDED setpoint -- the magnitude the live P2P automation
    # actually wrote to the inverter, reconstructed from the real
    # setpoint magnitude + real CMD direction (Charge/Discharge/Stop),
    # since the setpoint entity itself only ever holds an unsigned kW
    # magnitude (see this project's own documented "stale setpoint" HA
    # YAML gotcha -- CMD, not the setpoint value alone, decides what the
    # inverter actually does).
    setpoint_hist = fetch_history_range(
        "number.logger_charging_discharging_power_kw", day_start, day_end
    )
    cmd_hist = fetch_history_range(
        "sensor.logger_charging_discharging_command", day_start, day_end
    )
    # 2026-08-20: migrated off guerrier onto our own project-owned
    # equivalents (see nimbus_solver_forecast_writer.py's matching change
    # and CLAUDE.md's Aug 20 session log for the full investigation).
    import_price_hist = fetch_history_range(
        "sensor.localvolts_costs_flex_up", day_start, day_end
    )
    export_price_hist = fetch_history_range(
        "sensor.localvolts_earnings_flex_up", day_start, day_end
    )
    soc_hist = fetch_history_range(
        "sensor.logger_battery_level_soc", day_start - timedelta(hours=6), day_end
    )

    # Real, already-documented unit bug (this project's own Nimbus
    # Solver CLAUDE.md, "Real units bug"): sensor.combined_total_dc_power
    # reports in W, not kW (confirmed live 2026-08-17: state=9998.0,
    # unit_of_measurement="W") -- a caller reading its raw state directly
    # as kW overstates real solar-driven revenue by exactly 1000x, which
    # is precisely what produced an impossible J_ref/J_star (-$516/day)
    # on this script's own first real diagnostic run against live NUC1
    # data. Divided by 1000 here, matching the same real fix already
    # applied in the sibling reconciliation script.
    solar_kw = [
        max(0.0, v / 1000.0) for v in resample_nearest_float(solar_hist, grid_times)
    ]
    load_kw = [max(0.0, v) for v in resample_nearest_float(load_hist, grid_times)]

    # Real fix (2026-08-22, direct Mark Purcell finding): the oracle
    # (J*) used to be built completely FREE to retime the real settled
    # P2P volume to whichever periods had the best combined spot+bonus
    # economics (export_bonus_price/export_bonus_volume_kwh below) --
    # a degree of freedom the REAL controller never actually has, since
    # the real P2P deal is a flat, pre-committed rate for the whole
    # window, not something that can be re-timed even with perfect
    # price knowledge (see solver.elements.GridConfig.fixed_export_kw's
    # own docstring, nimbus repo, for the full "consistency of delivery
    # is itself part of what earns the rate" reasoning -- the exact
    # same real constraint nimbus_solver_forecast_writer.py's own
    # forward-looking plan already respects, just never applied here).
    # Mark's own words: "why is it missing so much of the mark? Prices
    # aren't varying that much" -- a real, structural mismatch between
    # what the oracle was allowed to do and what the real controller was
    # ever bound by, not genuine recoverable regret.
    #
    # Reconstructed from the REAL historical input_number.p2p_grid_
    # export_target_kw value that was actually live during THIS specific
    # day's own P2P window (not today's current value, in case it was
    # ever changed) -- same real, honest "ground truth from history, not
    # assumption" discipline as every other input in this script.
    p2p_target_hist = fetch_history_range(
        "input_number.p2p_grid_export_target_kw", day_start, day_end
    )
    p2p_window_start = day_start.replace(hour=17, minute=0, second=0, microsecond=0)
    # robust_value_near, not value_at_or_before: a plain point-in-time
    # lookup at exactly the window's own start is fragile against a
    # real, brief, self-correcting transient in this input_number
    # landing on that exact instant -- see robust_value_near()'s own
    # docstring for the full incident this was found from.
    real_p2p_target_kw = robust_value_near(
        p2p_target_hist, p2p_window_start, window_seconds=300.0, default=11.5
    )
    # Second real structural mismatch, same class as the P2P-window fix
    # above -- found chasing the remaining $11.23 regret after the first
    # fix (tracking_cost was confirmed tiny, $0.04, ruling out execution
    # gap; this was the next real candidate). The real automation is
    # ALSO hard-locked into Self-Consume (zero export) for
    # SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE hours after every P2P
    # window closes (config/automations.yaml's own p2p_battery_sell_end_
    # midnight / p2p_haeo_resume_at_4am, 00:00-04:00 sharp, deterministic
    # -- see nimbus_solver_forecast_writer.py's own matching constant for
    # the full "why a hard constraint, not a soft nudge" reasoning) --
    # the oracle here was free to export during those same early-morning
    # hours if real prices happened to make it look profitable, another
    # real freedom the real controller never had. Hours 0-3 of THIS
    # scored day are the self-consume window following the PREVIOUS
    # night's own P2P close, so pinning them here (not "hours 0-3 of the
    # day after") is the correct, real mapping.
    SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE = 4
    oracle_fixed_export_kw = [
        real_p2p_target_kw
        if 17 <= grid_times[i].hour < 24
        else (
            0.0
            if grid_times[i].hour < SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE
            else float("nan")
        )
        for i in range(N_PERIODS)
    ]

    actual_net_kw = resample_nearest_float(battery_actual_hist, grid_times)
    actual_charge_kw = np.array([max(0.0, -v) for v in actual_net_kw])
    actual_discharge_kw = np.array([max(0.0, v) for v in actual_net_kw])

    setpoint_kw = resample_nearest_float(setpoint_hist, grid_times)
    cmd = resample_nearest_str(cmd_hist, grid_times, default=CMD_CODE_STOP_DEFAULT)
    commanded_net_kw = [
        setpoint_kw[i]
        if cmd[i] == CMD_CODE_DISCHARGE
        else (-setpoint_kw[i] if cmd[i] == CMD_CODE_CHARGE else 0.0)
        for i in range(N_PERIODS)
    ]
    commanded_charge_kw = np.array([max(0.0, -v) for v in commanded_net_kw])
    commanded_discharge_kw = np.array([max(0.0, v) for v in commanded_net_kw])

    spot_import_raw = resample_nearest_float(
        import_price_hist, grid_times, default=0.20
    )
    spot_export = resample_nearest_float(export_price_hist, grid_times, default=0.05)
    import_price = [
        spot_import_raw[i] + network_energy_rate(grid_times[i].hour) + CERTIFICATES_RATE
        for i in range(N_PERIODS)
    ]
    bonus_price = fetch_real_p2p_rates_for_day(yesterday, grid_times)
    if bonus_price is None:
        print(
            f"[{now.isoformat()}] {day_key} real P2P rate unavailable -- skipping, will retry next run",
            file=sys.stderr,
        )
        return

    # Fine-grid tracking fidelity/cost (2026-08-18, see FINE_PERIOD_HOURS'
    # own comment above) -- resamples the SAME already-fetched raw
    # history (setpoint_hist/cmd_hist/battery_actual_hist/export_price_
    # hist, no new API calls) onto a 1-min grid instead of the coarse
    # 96-period one, specifically for solver.tracking's two functions.
    # Everything else in this file (the LP-solve-dependent EPR/regret
    # path below) still uses the coarse grid_times/period_hours_arr --
    # only this one, LP-solve-free computation benefits from going
    # finer, so only this one does.
    fine_grid_times = [
        day_start + timedelta(hours=i * FINE_PERIOD_HOURS)
        for i in range(N_FINE_PERIODS)
    ]
    fine_hours_arr = np.full(N_FINE_PERIODS, FINE_PERIOD_HOURS)
    fine_setpoint_kw = resample_nearest_float(setpoint_hist, fine_grid_times)
    fine_cmd = resample_nearest_str(
        cmd_hist, fine_grid_times, default=CMD_CODE_STOP_DEFAULT
    )
    fine_commanded_net_kw = np.array(
        [
            fine_setpoint_kw[i]
            if fine_cmd[i] == CMD_CODE_DISCHARGE
            else (-fine_setpoint_kw[i] if fine_cmd[i] == CMD_CODE_CHARGE else 0.0)
            for i in range(N_FINE_PERIODS)
        ]
    )
    fine_actual_net_kw = np.array(
        resample_nearest_float(battery_actual_hist, fine_grid_times)
    )
    fine_export_price = np.array(
        resample_nearest_float(export_price_hist, fine_grid_times, default=0.05)
    )

    fine_tracking = compute_tracking_fidelity(
        hours=fine_hours_arr,
        commanded_kw=fine_commanded_net_kw,
        actual_kw=fine_actual_net_kw,
    )
    fine_tracking_cost = tracking_error_cost(
        hours=fine_hours_arr,
        commanded_kw=fine_commanded_net_kw,
        actual_kw=fine_actual_net_kw,
        export_price=fine_export_price,
    )

    # Real, confirmed fix (2026-08-17): every one of these used to read a
    # HAEO-integration-owned number.* entity (confirmed live via
    # integration_entities('haeo')) -- direct violation of this project's
    # own PERMANENT "Nimbus must never reference any HAEO sensor/entity"
    # directive; if HAEO were ever removed this script would break
    # entirely. Now reads genuinely independent input_number helpers
    # (config/integrations/nimbus_solver_battery_config.yaml, sibling
    # repo) -- see nimbus_solver_forecast_writer.py's own matching fix for
    # the full incident.
    capacity_kwh = num("input_number.nimbus_solver_battery_capacity_kwh")
    max_charge_kw = num("input_number.nimbus_solver_battery_max_charge_kw")
    max_discharge_kw = ha_get("number.logger_charging_discharging_power_kw")[
        "attributes"
    ]["max"]  # not HAEO -- plain Modbus-backed template number, no owning integration
    charge_cost = num("input_number.nimbus_solver_battery_charge_cost")
    discharge_cost_arr = np.array(
        [battery_discharge_cost_rate(t.hour) for t in grid_times]
    )
    # Zero, not BATTERY_SALVAGE_VALUE_NIGHT/OTHER -- see the real, verified
    # "invalid EPR (>100%, negative regret)" fix documented above this
    # function's own battery_cfg construction.
    salvage_value = 0.0
    import_limit_kw = num("input_number.nimbus_solver_grid_import_limit_kw")
    export_limit_kw = num("input_number.nimbus_solver_grid_export_limit_kw")

    min_pct = num("input_number.nimbus_solver_battery_min_soc_pct")
    max_pct = num("input_number.nimbus_solver_battery_max_soc_pct")
    initial_pct = value_at_or_before(soc_hist, day_start, default=50.0)
    final_pct = value_at_or_before(
        soc_hist, day_end - timedelta(seconds=1), default=initial_pct
    )
    initial_soc_kwh = capacity_kwh * initial_pct / 100.0
    final_soc_kwh_actual = capacity_kwh * final_pct / 100.0

    battery_cfg = elements.BatteryConfig(
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=capacity_kwh * min_pct / 100.0,
        max_soc_kwh=capacity_kwh * max_pct / 100.0,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        charge_efficiency=0.999,
        discharge_efficiency=0.999,
        charge_cost=charge_cost,
        discharge_cost=discharge_cost_arr,
        salvage_value=salvage_value,
    )
    grid_residual = elements.GridConfig(
        import_price=np.array(import_price),
        export_price=np.array(spot_export),
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
    )
    # Real fix (see the real_p2p_target_kw/oracle_fixed_export_kw
    # comment above): the oracle now honours the SAME flat, pre-
    # committed P2P rate constraint the real controller was actually
    # bound by, instead of being free to retime the settled volume
    # to whichever periods looked best in hindsight.
    grid_oracle = elements.GridConfig(
        import_price=np.array(import_price),
        export_price=np.array(spot_export),
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
        export_bonus_price=np.array(bonus_price),
        export_bonus_volume_kwh=real_p2p_volume_kwh,
        fixed_export_kw=np.array(oracle_fixed_export_kw),
    )
    solar_cfg = elements.SolarConfig(forecast_kw=np.array(solar_kw))
    load_cfg = elements.LoadConfig(name="whole_house", forecast_kw=np.array(load_kw))
    periods = elements.PeriodGrid(hours=np.array(period_hours_arr), start=grid_times[0])

    # Real, ongoing before/after comparison (2026-08-22) -- the OLD
    # (unconstrained) oracle recomputed alongside the new, fixed one on
    # EVERY run, logged only, never used for the actual pushed score.
    # This is deliberate: a one-off diagnostic would only prove the fix
    # helps on a single historical day; logging both every single day
    # going forward gives a real, ongoing signal for how much of past
    # regret readings were genuinely structural (the P2P timing-freedom
    # mismatch this fix closes) versus real, still-recoverable
    # inefficiency -- exactly the distinction Mark's own question was
    # getting at.
    grid_oracle_unfixed = elements.GridConfig(
        import_price=np.array(import_price),
        export_price=np.array(spot_export),
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
        export_bonus_price=np.array(bonus_price),
        export_bonus_volume_kwh=real_p2p_volume_kwh,
    )
    report_unfixed = compute_quality_report(
        periods=periods,
        grid_residual=grid_residual,
        grid_oracle=grid_oracle_unfixed,
        battery=battery_cfg,
        solar=solar_cfg,
        load=load_cfg,
        timestamps=grid_times,
        real_p2p_dollars_earned=real_p2p_dollars,
        commanded_charge_kw=commanded_charge_kw,
        commanded_discharge_kw=commanded_discharge_kw,
        actual_charge_kw=actual_charge_kw,
        actual_discharge_kw=actual_discharge_kw,
        final_soc_kwh_actual=final_soc_kwh_actual,
    )
    regret_dollars_unfixed = report_unfixed.j_ach - report_unfixed.j_star

    report = compute_quality_report(
        periods=periods,
        grid_residual=grid_residual,
        grid_oracle=grid_oracle,
        battery=battery_cfg,
        solar=solar_cfg,
        load=load_cfg,
        timestamps=grid_times,
        real_p2p_dollars_earned=real_p2p_dollars,
        commanded_charge_kw=commanded_charge_kw,
        commanded_discharge_kw=commanded_discharge_kw,
        actual_charge_kw=actual_charge_kw,
        actual_discharge_kw=actual_discharge_kw,
        final_soc_kwh_actual=final_soc_kwh_actual,
    )

    regret_dollars = (
        report.j_ach - report.j_star
    )  # positive = actual cost MORE than perfect foresight, i.e. real $ left on the table
    day_entry = {
        "epr": round(report.epr.epr, 4),
        "theoretical_maximum_yield": round(report.epr.theoretical_maximum_yield, 4),
        "value_captured": round(report.epr.value_captured, 4),
        "uplift_available": round(report.epr.uplift_available, 4),
        "j_ref": round(report.j_ref, 4),
        "j_ach": round(report.j_ach, 4),
        "j_star": round(report.j_star, 4),
        "regret_dollars": round(regret_dollars, 4),
        # Fine-grid (1-min) tracking_fidelity/tracking_cost, replacing
        # the coarse 96-period figures previously here -- same field
        # names (no dashboard change needed), materially more accurate
        # for this specific purpose (see FINE_PERIOD_HOURS' own comment
        # above). Real, deliberate methodology change -- expect a real
        # discontinuity in the rolling history right at the date this
        # deployed, same as this project's own prior "state schema
        # version bump" moments elsewhere (e.g. lv_p2p_forecast_writer.py
        # v2->v3), not a bug if the trend line jumps here.
        "tracking_fidelity": round(fine_tracking.tracking_fidelity, 4),
        "tracking_cost": round(fine_tracking_cost, 4),
        "worst_gap_index": fine_tracking.worst_gap_index,  # now a 1-min-grid index (0-1439), not the old 15-min one (0-95)
        "worst_gap_at_local": fine_grid_times[fine_tracking.worst_gap_index].isoformat()
        if fine_tracking.n_samples > 0
        else None,
        "worst_gap_kw": round(fine_tracking.worst_gap_kw, 3),
        "mean_absolute_error_kw": round(fine_tracking.mean_absolute_error_kw, 3),
        "energy_shortfall_kwh": round(fine_tracking.energy_shortfall_kwh, 3),
        "real_p2p_dollars": round(real_p2p_dollars, 4),
        "real_p2p_volume_kwh": round(real_p2p_volume_kwh, 3),
    }
    quality_history[day_key] = day_entry
    save_quality_history(quality_history)

    hourly_regret_rounded = {
        str(k): round(v, 4) for k, v in report.hourly_regret.items()
    }
    ha_post_state(
        ENTITY_ID,
        day_entry["epr"],
        {
            "unit_of_measurement": None,
            "friendly_name": "Nimbus Solver Quality Report (EPR)",
            "latest_date": day_key,
            "history": quality_history,
            "hourly_regret_latest_day": hourly_regret_rounded,
            "generated_at": now.isoformat(),
            **day_entry,
        },
    )
    print(
        f"[{now.isoformat()}] scored {day_key}: EPR={day_entry['epr']:.3f} "
        f"regret=${regret_dollars:.2f} tracking_fidelity={day_entry['tracking_fidelity']:.3f} "
        f"real_p2p=${real_p2p_dollars:.2f}/{real_p2p_volume_kwh:.1f}kWh "
        f"j_ref={report.j_ref:.2f} j_ach={report.j_ach:.2f} j_star={report.j_star:.2f} "
        f"| oracle fixed-export fix: regret was ${regret_dollars_unfixed:.2f} (free P2P retiming) "
        f"vs ${regret_dollars:.2f} now (real, honoured constant-rate constraint) "
        f"-- ${regret_dollars_unfixed - regret_dollars:.2f} of that was structural, not recoverable"
    )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(
            f"HTTP error: {e.code} {e.read().decode('utf-8', errors='replace')}",
            file=sys.stderr,
        )
        raise
