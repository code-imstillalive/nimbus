"""Part D scenarios: real NEMWEB market inputs.

Spec 000 Part D. Nimbus never reads NEMWEB itself. It reads three things a
NEM household's Home Assistant holds, and each is built here from committed
NEMWEB files in ``tests/golden/nemweb/<folder>/``:

- the regional spot forecast sensor (``solver_regional_spot_forecast_sensor``,
  a ``sensor.nem_pd7day_<region>_nem_spot_price_forecast``): committed as
  produced by nem_pd7day's own parsers and golden harness from the folder's
  PD7DAY run, in two variants (``scripts/golden_nemweb.py forecast``):
  ``passthrough`` (empty calibration store, ``calibrated`` equals the raw
  PD7DAY price) and ``fitted`` (nem_pd7day's synthetic observation seed);
- the regional spot price sensor's recorded history
  (``solver_regional_spot_current_price_sensor``): the region's real
  5-minute TradingIS prices before the frozen instant;
- the retail import and export price sensors and their history: TradingIS
  spot plus a fixed retail margin (``RETAIL_MARGIN``), so the 5-minute
  offset ``compute_5min_offset`` learns is known exactly.

The household (battery, load) is the synthetic Part A household. Only the
market is real.
"""

from __future__ import annotations

import copy
import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from golden.fake_ha import FakeHA, HistoryPoint
from golden.scenarios import Scenario, register
from golden.scenarios_synthetic import BASE_CONFIG, LOAD

NEMWEB = Path(__file__).parent / "nemweb"
NEM = datetime.fromisoformat("2026-01-01T00:00:00+10:00").tzinfo

RETAIL_MARGIN = 0.12  # $/kWh added to spot for the retail import price
SPOT_FORECAST = "sensor.golden_nem_spot_price_forecast"
SPOT_NOW = "sensor.golden_regional_spot_price"
PRICE_ARRAY = "sensor.golden_price_forecast_array"
IMPORT = "sensor.fake_import_price"
EXPORT = "sensor.fake_export_price"
HISTORY_DAYS = 14


def tradingis(folder: str, region: str) -> list[tuple[datetime, float]]:
    """(interval start, $/kWh) for every committed TradingIS row, sorted."""
    path = NEMWEB / folder / f"TRADINGIS_{region}_history.price.csv.gz"
    out = []
    for line in gzip.decompress(path.read_bytes()).decode("utf-8").splitlines():
        cols = line.split(",")
        if cols[0] != "D" or cols[6] != region:
            continue
        end = datetime.strptime(cols[4].strip('"'), "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=NEM
        )
        out.append((end - timedelta(minutes=5), float(cols[8]) / 1000.0))
    return sorted(out)


def spot_forecast(folder: str, variant: str) -> dict:
    path = NEMWEB / folder / f"nem_pd7day_forecast.{variant}.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


def _iso_utc(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def _load_forecast(now: datetime) -> dict:
    """A deterministic 96 h household load profile, 30-minute points, kW."""
    shape = (
        0.6, 0.55, 0.5, 0.5, 0.5, 0.55, 0.9, 1.4, 1.2, 0.9, 0.8, 0.8,
        0.8, 0.8, 0.9, 1.1, 1.6, 2.4, 2.8, 2.5, 1.9, 1.4, 1.0, 0.8,
    )  # fmt: skip
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    pts = []
    for i in range(96 * 2 + 2):
        t = start + timedelta(minutes=30 * i)
        pts.append(
            {
                "time": t.astimezone(NEM).isoformat(),
                "value": shape[t.astimezone(NEM).hour],
            }
        )
    return {
        "state": str(pts[0]["value"]),
        "attributes": {"unit_of_measurement": "kW", "forecast": pts},
    }


def build(folder: str, region: str, now_iso: str, variant: str, overrides: dict):
    now = datetime.fromisoformat(now_iso)
    prices = tradingis(folder, region)
    past = [
        (t, v) for t, v in prices if t <= now and t > now - timedelta(days=HISTORY_DAYS)
    ]
    current = past[-1][1]
    fc = spot_forecast(folder, variant)

    config = copy.deepcopy(BASE_CONFIG)
    config.update(
        {
            "solver_price_forecast_array_sensor": PRICE_ARRAY,
            "solver_regional_spot_forecast_sensor": SPOT_FORECAST,
            "solver_regional_spot_current_price_sensor": SPOT_NOW,
        }
    )
    config.update(overrides)

    # The retail forecast array covers only the current and next trading
    # interval, from the PD7DAY raw price, so every later period reaches the
    # LP through the AEMO anchor (fetch_aemo_forecast + compute_5min_offset),
    # which is the path Part D exists for.
    first = sorted(fc["attributes"]["forecast"], key=lambda p: p["time"])[:2]
    array = []
    for p in first:
        t0 = datetime.fromisoformat(p["time"])
        for k in range(6):
            t = t0 + timedelta(minutes=5 * k)
            array.append(
                {
                    "time": t.isoformat(),
                    "costsflexup": round(p["raw_value"] + RETAIL_MARGIN, 6),
                    "earningsflexup": round(p["raw_value"], 6),
                }
            )

    def hist(offset: float) -> list[HistoryPoint]:
        return [HistoryPoint(_iso_utc(t), repr(round(v + offset, 6))) for t, v in past]

    states = {
        "sensor.nimbus_solver_config": {"state": "configured", "attributes": config},
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        IMPORT: {"state": repr(round(current + RETAIL_MARGIN, 6)), "attributes": {}},
        EXPORT: {"state": repr(round(current, 6)), "attributes": {}},
        SPOT_NOW: {
            "state": repr(round(current, 6)),
            "attributes": {"unit_of_measurement": "$/kWh"},
        },
        SPOT_FORECAST: {"state": fc["state"], "attributes": fc["attributes"]},
        PRICE_ARRAY: {
            "state": array[0]["costsflexup"],
            "attributes": {"forecast": array},
        },
        LOAD: _load_forecast(now),
    }
    history = {IMPORT: hist(RETAIL_MARGIN), EXPORT: hist(0.0), SPOT_NOW: hist(0.0)}
    return FakeHA(states=states, history=history)


def _register(name, folder, region, now_iso, variant, purpose, overrides=None):
    register(
        Scenario(
            name=name,
            instant=now_iso,
            build=lambda: build(folder, region, now_iso, variant, overrides or {}),
            purpose=purpose,
        )
    )


# (scenario name stem, folder, region, frozen instant NEM time, what it pins)
FOLDERS = (
    (
        "qld_nemweb_short_lead_spike",
        "QLD1",
        "2026-08-05T07:31:00+10:00",
        "QLD1 PD7DAY 07:30 run: two cap-scale forecasts under 24 h out and 21 beyond, none of which arrived",
    ),
    (
        "nsw_nemweb_days27_endeavour",
        "NSW1",
        "2026-08-05T07:31:00+10:00",
        "NSW1, same run: price-cap forecasts beyond 24 h, none of which arrived",
    ),
    (
        "vic_nemweb_spike_interconnectors",
        "VIC1",
        "2026-07-30T07:31:00+10:00",
        "VIC1 PD7DAY 07:30 run: forecasts to the $23,200/MWh cap from 11.5 h out",
    ),
    (
        "sa_nemweb_cap_plateau",
        "SA1",
        "2026-07-30T07:31:00+10:00",
        "SA1, same run: cap plateaus on 31 July that never arrived",
    ),
)

for stem, region, now_iso, purpose in FOLDERS:
    for variant in ("passthrough", "fitted"):
        _register(
            f"{stem}_{variant}",
            stem,
            region,
            now_iso,
            variant,
            f"{purpose}; nem_pd7day calibration {variant}",
        )

# The one real spike in the harvest window: SA1, $4,981/MWh for the interval
# ending 02:35 on 31 July (NEM time), $3,844/MWh ending 02:55. Frozen at
# 02:31, inside the first of them, with the price spike override armed.
# The latest PD7DAY run before it (17:40 on 30 July) forecast neither.
SPIKE_OVERRIDE = {
    "solver_price_spike_threshold": 1.0,
    "solver_price_spike_override_armed": True,
    "solver_price_spike_discharge_kw": 5.0,
}
for variant in ("passthrough", "fitted"):
    _register(
        f"sa_nemweb_spike_arrived_{variant}",
        "sa_nemweb_spike_arrived",
        "SA1",
        "2026-07-31T02:31:00+10:00",
        variant,
        "SA1 02:30 interval at $4,981/MWh, spike override armed; "
        f"nem_pd7day calibration {variant}",
        SPIKE_OVERRIDE,
    )
