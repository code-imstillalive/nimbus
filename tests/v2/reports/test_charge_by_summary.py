"""The published plan's "charge by" summary (household, 14 Oct 2026) (#2393).

`solver_shared.charge_by_summary` had no test: main's CI never reached the
path that runs when the setting is on. It reads the plan's own rows: the SoC
at the end of the last period that ends by the deadline, and how far short
of the target that is. On a site with more than one battery it reads the home
battery's own rows (16 Oct 2026: a fleet blend of home and EV read "short").
Synthetic plans only.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import solver_shared

START = datetime(2026, 9, 20, 12, 0, tzinfo=solver_shared.LOCAL_TZ)
KEYS = {
    "charge_by_time",
    "charge_by_target_pct",
    "charge_by_next",
    "charge_by_next_soc_pct",
    "charge_by_next_short_pct",
}
CFG = {"solver_charge_by_percent": 90, "solver_charge_by_hour": 16}


def plan(socs):
    """Hourly rows from 12:00 local; soc_pct is the SoC at each row's end."""
    return [
        {"time": (START + timedelta(hours=i)).isoformat(), "hours": 1.0, "soc_pct": s}
        for i, s in enumerate(socs)
    ]


FLEET = plan([50, 60, 70, 80, 90, 95])  # the 15:00 row ends at the 16:00 deadline


def test_off_every_key_is_present_and_none():
    out = solver_shared.charge_by_summary({}, FLEET)
    assert out == dict.fromkeys(KEYS)
    assert solver_shared.charge_by_summary(
        {"solver_charge_by_percent": 0}, FLEET
    ) == dict.fromkeys(KEYS)


def test_the_soc_at_the_deadline_and_the_shortfall_are_read_from_the_plan():
    out = solver_shared.charge_by_summary(CFG, FLEET)
    assert out == {
        "charge_by_time": "16:00",
        "charge_by_target_pct": 90.0,
        "charge_by_next": (START + timedelta(hours=4)).isoformat(),
        "charge_by_next_soc_pct": 80.0,
        "charge_by_next_short_pct": 10.0,
    }
    # Above the target is never a negative shortfall; a half hour reads as such.
    out = solver_shared.charge_by_summary(
        {"solver_charge_by_percent": 75, "solver_charge_by_hour": 16.5}, FLEET
    )
    assert (out["charge_by_time"], out["charge_by_next_short_pct"]) == ("16:30", 0.0)


def test_with_more_than_one_battery_the_home_batterys_own_rows_are_read():
    home = {"name": "home", "forecast": plan([60, 70, 80, 99, 100, 100])}
    ev = {"name": "ev", "forecast": plan([10, 20, 30, 40, 50, 60])}
    out = solver_shared.charge_by_summary(CFG, FLEET, [ev, home])
    assert (out["charge_by_next_soc_pct"], out["charge_by_next_short_pct"]) == (
        99.0,
        0.0,
    )

    # Home rows that stop before the deadline give no SoC, not the fleet's.
    short = {"name": "home", "forecast": plan([60, 70])}
    out = solver_shared.charge_by_summary(CFG, FLEET, [ev, short])
    assert out["charge_by_next"] is not None
    assert out["charge_by_next_soc_pct"] is None

    # One battery: the fleet figure is the home battery's.
    out = solver_shared.charge_by_summary(CFG, FLEET, [home])
    assert out["charge_by_next_soc_pct"] == 80.0


def test_no_deadline_in_the_plan_or_an_unreadable_row_gives_no_next():
    late = {"solver_charge_by_percent": 90, "solver_charge_by_hour": 23}
    unreadable = [*FLEET[:2], {"hours": 1.0, "soc_pct": 70}, *FLEET[3:]]
    for cfg, rows in ((late, FLEET), (CFG, unreadable), (CFG, [])):
        out = solver_shared.charge_by_summary(cfg, rows)
        assert out["charge_by_time"] is not None
        assert out["charge_by_next"] is None
        assert out["charge_by_next_soc_pct"] is None
