"""nimbus #2257: the live site import limit.

The plan keeps battery plus *forecast* load under the Solver's Grid Max
Import, but nothing compared the real meter at the moment the battery was
told what to do. On 4 Nov 2026 the reference household's supply (about
46 kW) tripped: a 30 kW grid charge, the EV charger (10.7 kW), hot water
and pool (5.5 kW), then a toaster. Mark Purcell, on #2257: the guard has to
read the live meter, because Nimbus neither commands nor forecasts every
load that can come on (Cottonwood's EV charger is driven elsewhere).

`allowed_charge_kw` is the most the battery may charge right now without
grid import passing the limit: what it is charging now (negative while it
discharges), plus the headroom the meter shows. Import only in this first slice (Mark: export is a
fast-follow once this is proven).

HA-import-free, like power_units.py, so it is tested directly.
"""

from __future__ import annotations


def allowed_charge_kw(
    limit_kw: float | None,
    grid_kw: float | None,
    battery_kw: float | None,
    *,
    battery_positive_is_charge: bool,
    grid_positive_is_import: bool = True,
) -> float | None:
    """The battery's allowed charge power in kW, or None when it cannot be
    known: no limit set (0 = off), or a reading missing. Never a guessed 0.

    `grid_kw` and `battery_kw` are the meters' own readings in kW, in their
    own sign conventions, which the two flags describe."""
    if limit_kw is None or limit_kw <= 0 or grid_kw is None or battery_kw is None:
        return None
    importing = grid_kw if grid_positive_is_import else -grid_kw
    charging = battery_kw if battery_positive_is_charge else -battery_kw
    # `charging` is not clamped: a battery discharging now (negative) stops
    # supplying the house when it switches to charge, so that discharge counts
    # against the room too (Mark Purcell, #2258: discharging 10 kW at 20 kW
    # import against 44 allows 14, not 24).
    return max(0.0, charging + limit_kw - importing)
