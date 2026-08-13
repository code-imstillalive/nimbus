"""Feature engineering for Nimbus's load model.

Ported unchanged (in spirit) from the 116KAT project's own Phase 1 script
(116KAT-HA-AI/scripts/smart_load_forecaster.py) -- cyclic sin/cos calendar
encodings so e.g. 23:00 and 00:00 land numerically close together, instead of
being maximally far apart the way a raw "hour" integer would put them.

Deliberately entity-agnostic and timezone-agnostic here: callers pass in a
tz-aware local datetime directly (the integration layer is responsible for
converting from HA's stored UTC timestamps using the instance's own
configured timezone, rather than this module assuming any particular offset
the way the 116KAT-specific Phase 1 script did).
"""

from __future__ import annotations

import math
from datetime import datetime

FEATURE_NAMES: list[str] = [
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "month_sin", "month_cos", "is_weekend", "temp_c",
]


def build_features(local_dt: datetime, temp_c: float) -> list[float]:
    """Build one feature row for a single point in time.

    Args:
        local_dt: A datetime already in the target home's local timezone.
        temp_c: Temperature at (or forecast for) this point, in Celsius.

    """
    hour_frac = local_dt.hour + local_dt.minute / 60.0
    dow = local_dt.weekday()  # 0 = Monday
    month = local_dt.month
    return [
        math.sin(2 * math.pi * hour_frac / 24.0),
        math.cos(2 * math.pi * hour_frac / 24.0),
        math.sin(2 * math.pi * dow / 7.0),
        math.cos(2 * math.pi * dow / 7.0),
        math.sin(2 * math.pi * month / 12.0),
        math.cos(2 * math.pi * month / 12.0),
        1.0 if dow >= 5 else 0.0,
        temp_c,
    ]
