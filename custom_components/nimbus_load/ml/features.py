"""Feature engineering for Nimbus's load model.

Cyclic sin/cos calendar encodings so e.g. 23:00 and 00:00 land numerically
close together, instead of being maximally far apart the way a raw "hour"
integer would put them.

lag_short/lag_long/humidity added 2026-08-14 after real backtesting against
this project's own live history (30 days, 4 different loads): lag features
were consistently among the most important inputs for every load's model
(recent momentum matters -- "what was it doing 15/60 minutes ago"), and
humidity contributed meaningfully alongside temperature for weather-
sensitive loads. Both are optional (default 0.0) so this stays usable for
callers that genuinely have no lag history yet (very first prediction
after a fresh install) or no humidity sensor configured.

curtailment/in_schedule added the following morning, for two load
categories real/weather/lag features don't capture: a load run only to
soak up otherwise-curtailed solar (economically triggered, not by time or
temperature -- e.g. a pool heater used purely as a curtailment sink), and
a load on a genuine fixed daily timer (e.g. a pool pump running the same
hours every day) where a dedicated on/off window feature lets the model
learn the sharp boundary directly instead of only approximating it
through hour-of-day sin/cos splits. Both optional and off by default (0.0
curtailment, no schedule configured) -- most loads are neither.

Deliberately entity-agnostic and timezone-agnostic here: callers pass in a
tz-aware local datetime directly (the integration layer is responsible for
converting from HA's stored UTC timestamps using the instance's own
configured timezone).
"""

from __future__ import annotations

import math
from datetime import datetime

FEATURE_NAMES: list[str] = [
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "month_sin", "month_cos", "is_weekend", "temp_c",
    "humidity_pct", "lag_short", "lag_long", "curtailment", "in_schedule",
]


def build_features(
    local_dt: datetime,
    temp_c: float,
    humidity_pct: float = 0.0,
    lag_short: float = 0.0,
    lag_long: float = 0.0,
    curtailment: float = 0.0,
    schedule_start_hour: float | None = None,
    schedule_end_hour: float | None = None,
) -> list[float]:
    """Build one feature row for a single point in time.

    Args:
        local_dt: A datetime already in the target home's local timezone.
        temp_c: Temperature at (or forecast for) this point, in Celsius.
        humidity_pct: Relative humidity at (or forecast for) this point, 0-100.
        lag_short: The load's own value LAG_SHORT_STEPS grid-steps before
            this point (real recent history near-term; the model's own
            prior prediction further out in a forecast horizon).
        lag_long: Same, LAG_LONG_STEPS grid-steps before this point.
        curtailment: 1.0 if solar curtailment is (or is forecast to be)
            active at this point, else 0.0.
        schedule_start_hour: Start of this load's own fixed daily on-window
            (0-23), or None if it has no fixed schedule.
        schedule_end_hour: End of the window (exclusive), or None. Wraps
            past midnight correctly if end < start (e.g. 22 -> 6).
    """
    hour_frac = local_dt.hour + local_dt.minute / 60.0
    dow = local_dt.weekday()  # 0 = Monday
    month = local_dt.month

    if schedule_start_hour is None or schedule_end_hour is None:
        in_schedule = 0.0
    elif schedule_start_hour <= schedule_end_hour:
        in_schedule = 1.0 if schedule_start_hour <= hour_frac < schedule_end_hour else 0.0
    else:
        in_schedule = 1.0 if hour_frac >= schedule_start_hour or hour_frac < schedule_end_hour else 0.0

    return [
        math.sin(2 * math.pi * hour_frac / 24.0),
        math.cos(2 * math.pi * hour_frac / 24.0),
        math.sin(2 * math.pi * dow / 7.0),
        math.cos(2 * math.pi * dow / 7.0),
        math.sin(2 * math.pi * month / 12.0),
        math.cos(2 * math.pi * month / 12.0),
        1.0 if dow >= 5 else 0.0,
        temp_c,
        humidity_pct,
        lag_short,
        lag_long,
        curtailment,
        in_schedule,
    ]
