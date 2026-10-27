"""Layer 1 of the rebuilt suite (#1837), round 2: the features the first
generator never built. The same-mutant comparison on `_build_plan_once`
(old 30/30, new 4/30, posted on #1837) found the survivors sat in code the
sites in `test_plan_invariants.py` never reach: the import release valve,
taper curves, a spike near Min SoC, an away period at t=0, the solar
confidence band and the terminal-value checks.

Each property here forces its feature to bind: a price that makes the LP
push a battery as hard as it can, or a load the grid limit can't cover, so a
wrong bound or coefficient changes the plan rather than hiding behind a
slack constraint.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import lp
from solver import lp as _solver_lp
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    MIN_CHARGE_DISCHARGE_COST_SPREAD,
    AdequacyLoadConfig,
    AdequacyWindow,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import build_plan
from solver.p2p_export import realized_export_bonus_credit
from test_network_paths import _idle_battery
from test_plan_invariants import SETTINGS, TOL, _num, sites


def _solve(site, *, solar_band=None, plan_options=None, **battery_overrides):
    lower, upper = solar_band if solar_band is not None else (None, None)
    return build_plan(
        **(plan_options or {}),
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=site["export_limit"],
        ),
        batteries=[BatteryConfig(**{**site["battery"], **battery_overrides})],
        solar=SolarConfig(forecast_kw=site["solar"], lower_kw=lower, upper_kw=upper),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
        sheddable_loads=(
            [SheddableLoadConfig(**site["sheddable"])] if site["sheddable"] else None
        ),
    )


def _solve_with_adequacy(site):
    from test_plan_invariants import _solve as solve_all

    return solve_all(site)


@st.composite
def plain_sites(draw):
    """`sites()` without the adequacy and thermal loads (their penalties add
    objective terms the plan doesn't report on their own), and with the
    import limit sometimes below the load, so the release valve runs."""
    site = draw(sites())
    site["adequacy"] = None
    site["thermal"] = None
    if draw(st.booleans()):
        site["import_limit"] = round(
            float(site["load"].max()) * draw(_num(0.0, 0.9)), 4
        )
    return site


def _parts(site, plan):
    """What the plan's own flows cost at the input prices, plus the terms it
    reports separately."""
    b = site["battery"]
    h = site["hours"]
    grid = np.sum(
        plan.grid_import_kw * site["import_price"] * h
        - plan.grid_export_kw * site["export_price"] * h
    )
    wear = np.sum(
        plan.battery_charge_kw * b["charge_cost"] * h
        + plan.battery_discharge_kw * b["discharge_cost"] * h
    )
    # Cycle wear (Track B2): $/kWh of throughput in either direction.
    wear += b.get("degradation_cost_per_kwh", 0.0) * np.sum(
        (plan.battery_charge_kw + plan.battery_discharge_kw) * h
    )
    salvage = -b["salvage_value"] * plan.battery_soc_kwh[-1]
    shed = sum(
        float(np.sum(s.shed_kw * site["sheddable"]["shed_cost"] * h))
        for s in plan.sheddable_loads or []
    )
    return (
        grid
        + wear
        + salvage
        + shed
        + plan.grid_import_excess_penalty_cost
        + plan.battery_loss_tiebreak_cost
        + plan.battery_charge_earliness_cost
        + plan.soc_penalty_cost
    )


def _curve(lo, hi, flat_kw, end_kw, knee_share, rising):
    """A concave two-segment power curve over [lo, hi]: rising to a flat top
    (discharge tapering near empty) or flat then falling (charge tapering
    near full)."""
    knee = lo + knee_share * (hi - lo)
    if rising:
        return [(lo, end_kw), (knee, flat_kw), (hi, flat_kw)]
    return [(lo, flat_kw), (knee, flat_kw), (hi, end_kw)]


@st.composite
def push_sites(draw, direction):
    """A battery the prices push one way as hard as it can: paid to import
    (charge), or a rich export with room to sell (discharge). No load or
    solar, so nothing else competes for the flow."""
    n = draw(st.integers(min_value=1, max_value=12))
    width = draw(st.sampled_from([5 / 60, 0.25, 0.5, 1.0]))
    capacity = draw(_num(5.0, 40.0))
    lo = round(draw(_num(0.0, 0.2)) * capacity, 4)
    hi = round(draw(_num(0.8, 1.0)) * capacity, 4)
    flat = draw(_num(2.0, 15.0))
    curve = _curve(
        lo,
        hi,
        flat,
        round(flat * draw(_num(0.05, 0.9)), 4),
        draw(_num(0.2, 0.8)),
        rising=direction == "discharge",
    )
    start = lo if direction == "charge" else hi
    battery = {
        "name": "b",
        "capacity_kwh": capacity,
        "initial_soc_kwh": start,
        "min_soc_kwh": lo,
        "max_soc_kwh": hi,
        "max_charge_kw": flat,
        "max_discharge_kw": flat,
        "charge_efficiency": draw(_num(0.85, 0.99)),
        "discharge_efficiency": draw(_num(0.85, 0.99)),
        "charge_cost": 0.006,
        "discharge_cost": 0.006,
        "salvage_value": 0.0,
        f"{direction}_power_curve": curve,
    }
    if direction == "charge":
        imp, exp = np.full(n, -1.0), np.full(n, -2.0)
        battery["salvage_value"] = 0.5
    else:
        imp, exp = np.full(n, 5.0), np.full(n, 4.0)
    return {
        "hours": np.full(n, width),
        "import_price": imp,
        "export_price": exp,
        "import_limit": 2.0 * flat,
        "export_limit": 2.0 * flat,
        "load": np.zeros(n),
        "solar": np.zeros(n),
        "battery": battery,
        "sheddable": None,
        "curve": curve,
    }


def _flat_out(site, plan, direction):
    """Check every period against the cap at the SoC it starts from, and
    return the SoC that running flat out from the start would end on: the
    least of the inverter limit, the curve and the room left (charging), or
    of the inverter limit, the curve, the energy above Min SoC and the export
    limit (discharging). The steps are monotone in SoC, so no plan ends
    further than this."""
    b = site["battery"]
    socs, powers = zip(*site["curve"])
    h = site["hours"]
    flow = (
        plan.battery_charge_kw if direction == "charge" else plan.battery_discharge_kw
    )
    prev = b["initial_soc_kwh"]
    greedy = b["initial_soc_kwh"]
    for t in range(len(h)):
        assert flow[t] <= float(np.interp(prev, socs, powers)) + 1e-5, t
        prev = plan.battery_soc_kwh[t]
        curve = float(np.interp(greedy, socs, powers))
        if direction == "charge":
            room = (b["max_soc_kwh"] - greedy) / (b["charge_efficiency"] * h[t])
            rate = max(0.0, min(b["max_charge_kw"], curve, room))
            greedy += rate * b["charge_efficiency"] * h[t]
        else:
            left = (greedy - b["min_soc_kwh"]) * b["discharge_efficiency"] / h[t]
            cap = min(b["max_discharge_kw"], curve, left, site["export_limit"])
            greedy -= max(0.0, cap) / b["discharge_efficiency"] * h[t]
    return greedy


@SETTINGS
@given(st.sampled_from(["charge", "discharge"]), st.data())
def test_a_curve_flat_at_the_inverter_limit_is_no_curve(direction, data):
    """The power curves (household, 21 Aug: the CC->CV charge taper, the BMS
    caution near empty) are a ceiling under the inverter limit, never a
    loosening: a curve flat at the limit plans as well as no curve, within
    the solver's tie-break slack (#773; a few hundredths of a cent here) and
    to the kWh at the end. Not period by period: at a flat price, when it
    sells is a tie."""
    site = data.draw(push_sites(direction))
    b = site["battery"]
    flat = [
        (b["min_soc_kwh"], b["max_charge_kw"]),
        (b["max_soc_kwh"], b["max_charge_kw"]),
    ]
    with_flat = _solve(site, **{f"{direction}_power_curve": flat})
    without = _solve(site, **{f"{direction}_power_curve": None})
    assert with_flat.status == without.status == "optimal"
    assert with_flat.total_cost == pytest.approx(without.total_cost, rel=1e-4)
    assert with_flat.battery_soc_kwh[-1] == pytest.approx(
        without.battery_soc_kwh[-1], abs=1e-4
    )


@SETTINGS
@given(st.integers(min_value=2, max_value=12), st.integers(min_value=0, max_value=5))
def test_a_terminal_value_index_outside_the_grid_is_refused(n, past):
    """`terminal_value_period_indices` must lie on the grid: an index at or
    past n is refused, naming the grid's own range."""
    site = {
        "hours": np.full(n, 0.5),
        "import_price": np.full(n, 0.3),
        "export_price": np.full(n, 0.1),
        "import_limit": 10.0,
        "export_limit": 5.0,
        "load": np.ones(n),
        "solar": np.zeros(n),
        "sheddable": None,
        "battery": {
            "name": "b",
            "capacity_kwh": 10.0,
            "initial_soc_kwh": 5.0,
            "min_soc_kwh": 1.0,
            "max_soc_kwh": 9.0,
            "max_charge_kw": 3.0,
            "max_discharge_kw": 3.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "charge_cost": 0.006,
            "discharge_cost": 0.006,
            "salvage_value": 0.0,
        },
    }
    with pytest.raises(ValueError, match=rf"\(0\.\.{n - 1}\)"):
        _solve(
            site,
            terminal_value_breakpoints=[(8.0, 0.1)],
            terminal_value_period_indices=[n + past],
        )


def _tiered_value(above, tiers):
    """What a concave terminal value pays for `above` kWh over Min SoC: the
    dearest tier first, then the next."""
    total = 0.0
    for width, rate in tiers:
        take = min(width, max(0.0, above))
        total += take * rate
        above -= take
    return total


def _checkpoint_plan(n, export, indices):
    tiers = [(6.0, 0.60), (16.0, 0.25), (16.0, 0.05)]
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.50),
            export_price=np.full(n, export),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[
            BatteryConfig(
                name="battery",
                capacity_kwh=40.0,
                initial_soc_kwh=40.0,
                min_soc_kwh=2.0,
                max_soc_kwh=40.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
                charge_efficiency=0.97,
                discharge_efficiency=0.97,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.0,
                terminal_value_breakpoints=tiers,
                terminal_value_period_indices=indices,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 0.5))],
    )


@SETTINGS
@given(st.integers(min_value=2, max_value=12), st.integers(min_value=0, max_value=5))
def test_an_adequacy_deadline_past_the_plan_is_refused_naming_the_plan(n, past):
    """An adequacy load whose deadline lies at or past the plan's last
    period is refused before solving, and the message names the load, its
    deadline and the plan's own range."""
    deadline = n + past
    with pytest.raises(ValueError) as refused:
        build_plan(
            periods=PeriodGrid(np.full(n, 0.5)),
            grid=GridConfig(
                import_price=np.full(n, 0.3),
                export_price=np.full(n, 0.05),
                import_limit_kw=10.0,
                export_limit_kw=5.0,
            ),
            batteries=[
                BatteryConfig(
                    name="b",
                    capacity_kwh=10.0,
                    initial_soc_kwh=5.0,
                    min_soc_kwh=1.0,
                    max_soc_kwh=9.0,
                    max_charge_kw=3.0,
                    max_discharge_kw=3.0,
                    charge_efficiency=0.95,
                    discharge_efficiency=0.95,
                    charge_cost=0.006,
                    discharge_cost=0.006,
                    salvage_value=0.0,
                    available=False,
                )
            ],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
            adequacy_loads=[
                AdequacyLoadConfig(
                    name="pool",
                    max_power_kw=1.0,
                    target_kwh=1.0,
                    deadline_period=deadline,
                    shortfall_price=1.0,
                )
            ],
        )
    msg = str(refused.value)
    assert "'pool'" in msg and f"({deadline})" in msg and f"(0..{n - 1})" in msg


def _loss_battery(name, initial, eff, **over):
    return BatteryConfig(
        **{
            "name": name,
            "capacity_kwh": 40.0,
            "initial_soc_kwh": initial,
            "min_soc_kwh": 0.0,
            "max_soc_kwh": 40.0,
            "max_charge_kw": 20.0,
            "max_discharge_kw": 20.0,
            "charge_efficiency": eff,
            "discharge_efficiency": eff,
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "salvage_value": 0.0,
            **over,
        }
    )


def _one_period(price, *batteries):
    return build_plan(
        periods=PeriodGrid(np.ones(1)),
        grid=GridConfig(
            import_price=np.full(1, price),
            export_price=np.full(1, -0.01),
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        ),
        batteries=list(batteries),
        solar=SolarConfig(forecast_kw=np.zeros(1)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(1))],
    )


def _loss(price, rate):
    return min(price * rate, MIN_CHARGE_DISCHARGE_COST_SPREAD)


def _pool_plan(n, width, power, price, threshold, windows=None, earliest=0):
    return build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=price,
            export_price=np.full(n, 0.0),
            import_limit_kw=power + 5.0,
            export_limit_kw=5.0,
        ),
        batteries=[
            BatteryConfig(
                name="b",
                capacity_kwh=10.0,
                initial_soc_kwh=5.0,
                min_soc_kwh=1.0,
                max_soc_kwh=9.0,
                max_charge_kw=3.0,
                max_discharge_kw=3.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.006,
                discharge_cost=0.006,
                salvage_value=0.0,
                available=False,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="pool",
                max_power_kw=power,
                target_kwh=power * width,
                earliest_period=earliest,
                deadline_period=n - 1,
                shortfall_price=20.0,
                min_deferral_saving_dollars=threshold,
                windows=windows,
            )
        ],
    )


def _deferral_plan(n, width, price, threshold, spans, runs, calibrated=False):
    """A 1 kW load whose windows are `spans`, each owing `runs` whole
    periods; `threshold` None leaves the field unset."""
    windows = tuple(
        AdequacyWindow(earliest_period=a, deadline_period=b, target_kwh=runs * width)
        for a, b in spans
    )
    extra = {} if threshold is None else {"min_deferral_saving_dollars": threshold}
    return build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.asarray(price, dtype=float),
            export_price=np.zeros(n),
            import_limit_kw=10.0,
            export_limit_kw=10.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=1.0,
                target_kwh=runs * width,
                earliest_period=spans[0][0],
                deadline_period=spans[0][1],
                shortfall_price=20.0,
                windows=windows if len(spans) > 1 else None,
                **extra,
            )
        ],
        solve_options=lp.CalibratedOptions() if calibrated else None,
    )


@SETTINGS
@given(
    st.integers(1, 3),
    st.integers(0, 3),
    st.integers(0, 4),
    st.sampled_from([0.5, 1.0]),
    _num(0.05, 3.0),
    st.booleans(),
    st.booleans(),
)
def test_the_deferral_penalty_costs_its_stated_share_of_the_threshold(
    runs, start, slack, width, threshold, two, calibrated
):
    """#769 (Mark Purcell; household decision 15 Sep: "each load can have
    its own urgency then"). Each window prices lateness from its OWN
    opening at rate = (X / target) / window_hours, so the whole target at
    the deadline costs exactly X and at the opening nothing. At a flat
    price the load runs from each window's opening, and the plan costs that
    rate times the lateness it actually carries over the unset plan. A
    second window pulls to its own opening, not to the plan's start. The
    cost is a PRIMARY cost, on purpose allowed to beat real prices, so the
    calibrated solve (production's) charges exactly the same."""
    length = runs + slack
    spans = [(start, start + length - 1)]
    if two:
        spans.append((spans[0][1] + 2, spans[0][1] + 1 + length))
    n = spans[-1][1] + 2
    price = np.full(n, 0.20)
    base = _deferral_plan(n, width, price, None, spans, runs, calibrated)
    plan = _deferral_plan(n, width, price, threshold, spans, runs, calibrated)
    assert base.status == plan.status == "optimal"
    power = plan.adequacy_loads[0].power_kw
    want = 0.0
    for a, b in spans:
        np.testing.assert_allclose(power[a : a + runs], 1.0, atol=1e-6)
        np.testing.assert_allclose(power[a + runs : b + 1], 0.0, atol=1e-6)
        window_h = width * (b - a)
        if window_h > 0:
            rate = threshold / (runs * width) / window_h
            want += sum(rate * width * k * width for k in range(runs))
    assert plan.total_cost - base.total_cost == pytest.approx(want, abs=1e-6)


@SETTINGS
@given(st.integers(min_value=2, max_value=12), st.integers(min_value=0, max_value=5))
def test_an_adequacy_window_past_the_plan_is_refused_naming_the_plan(n, past):
    """A window whose deadline lies past the plan is refused before solving,
    and the message names the window's deadline and the plan's range."""
    deadline = n + past
    window = (
        AdequacyWindow(earliest_period=0, deadline_period=deadline, target_kwh=0.5),
    )
    with pytest.raises(ValueError) as refused:
        _pool_plan(n, 0.5, 1.0, np.full(n, 0.3), 0.0, windows=window)
    msg = str(refused.value)
    assert f"({deadline})" in msg and f"(0..{n - 1})" in msg


_NEAR_EMPTY = {
    "adequacy": None,
    "sheddable": None,
    "thermal": None,
    "hours": np.full(14, 0.25),
    "import_price": np.zeros(14),
    "export_price": np.zeros(14),
    "import_limit": 0.0,
    "export_limit": 1.0,
    "load": np.zeros(14),
    "solar": np.zeros(14),
    "battery": {
        "name": "b",
        "capacity_kwh": 23.0625,
        "initial_soc_kwh": 6.9188,
        "min_soc_kwh": 6.91875,
        "max_soc_kwh": 23.0625,
        "max_charge_kw": 1.0,
        "max_discharge_kw": 1.0,
        "charge_efficiency": 0.875,
        "discharge_efficiency": 0.875,
        "charge_cost": 0.005,
        "discharge_cost": 0.0625,
        "salvage_value": 0.0,
    },
}


def _run_once_plan(
    price, power, target, *, windows=None, allowed=None, semi=True, value=None
):
    n = len(price)
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.asarray(price, dtype=float),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=power,
                target_kwh=target,
                earliest_period=0,
                deadline_period=n - 1,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                windows=windows,
                allowed=allowed,
                value_per_kwh=value,
            )
        ],
        adequacy_semi_continuous=semi,
    )


def _runs(on):
    """The (start, end) of each unbroken run of True."""
    out, start = [], None
    for t, v in enumerate(list(on) + [False]):
        if v and start is None:
            start = t
        elif not v and start is not None:
            out.append((start, t - 1))
            start = None
    return out


def _thermal_plan(n, width, thermal, price=None):
    return build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, 0.5) if price is None else price,
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=5.0,
        ),
        batteries=[
            BatteryConfig(
                name="b",
                capacity_kwh=10.0,
                initial_soc_kwh=5.0,
                min_soc_kwh=1.0,
                max_soc_kwh=9.0,
                max_charge_kw=3.0,
                max_discharge_kw=3.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.006,
                discharge_cost=0.006,
                salvage_value=0.0,
                available=False,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
        thermal_loads=[ThermalLoadConfig(**thermal)],
    )


@SETTINGS
@given(
    plain_sites(),
    st.lists(_num(0.0, 0.5), min_size=24, max_size=24),
    _num(0.5, 10.0),
)
def test_the_export_bonus_is_claimed_on_real_export_within_its_cap(
    site, premiums, volume
):
    """The two-tier export bonus (P2P): a premium on top of the export price
    for up to `export_bonus_volume_kwh`. The bonus is only ever claimed on
    export that happened, the volume claimed stays within the cap, and the
    premium the plan collects is what the bonus's own settlement
    (`realized_export_bonus_credit`) pays for that export, the dearest
    periods first, and the plan's cost counts the premium exactly once."""
    n = len(site["hours"])
    bonus_price = np.array(premiums[:n])
    plan = build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=site["export_limit"],
            export_bonus_price=bonus_price,
            export_bonus_volume_kwh=volume,
        ),
        batteries=[BatteryConfig(**site["battery"])],
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
    )
    assert plan.status == "optimal"
    h = site["hours"]
    bonus = plan.export_bonus_kw
    assert (bonus >= -TOL).all()
    assert (bonus <= plan.grid_export_kw + 1e-6).all()
    assert float(np.sum(bonus * h)) <= volume + 1e-6
    collected = float(np.sum(bonus_price * bonus * h))
    grid = GridConfig(
        import_price=site["import_price"],
        export_price=site["export_price"],
        import_limit_kw=site["import_limit"],
        export_limit_kw=site["export_limit"],
        export_bonus_price=bonus_price,
        export_bonus_volume_kwh=volume,
    )
    settled = realized_export_bonus_credit(
        grid_export_kw=plan.grid_export_kw, hours=h, grid=grid, period_starts=None
    )
    assert abs(collected - settled) < 1e-5
    # The objective counts the premium once: the plan costs its flows less the
    # premium collected, up to the bonus's latest-preferred tie-break (a few
    # hundredths of a cent at most).
    site = {**site, "sheddable": None}
    assert abs(plan.total_cost - (_parts(site, plan) - collected)) < 1e-3


@SETTINGS
@given(
    st.integers(min_value=1, max_value=24),
    st.data(),
    st.one_of(st.none(), st.just(0.0)),
)
def test_the_export_bonus_settlement_pays_nothing_without_a_bonus_to_pay(
    n, data, volume
):
    """The bonus's settlement (`realized_export_bonus_credit`) pays nothing
    when no bonus is configured, or when its volume is zero, whatever was
    exported and whatever the premium."""
    export = np.array(data.draw(st.lists(_num(0.0, 10.0), min_size=n, max_size=n)))
    hours = np.array(data.draw(st.lists(_num(0.25, 1.0), min_size=n, max_size=n)))
    premium = np.array(data.draw(st.lists(_num(0.0, 0.5), min_size=n, max_size=n)))
    grid = GridConfig(
        import_price=np.full(n, 0.3),
        export_price=np.full(n, 0.05),
        import_limit_kw=10.0,
        export_limit_kw=10.0,
        export_bonus_price=None if volume is None else premium,
        export_bonus_volume_kwh=volume,
    )
    assert (
        realized_export_bonus_credit(
            grid_export_kw=export, hours=hours, grid=grid, period_starts=None
        )
        == 0.0
    )


# --- #1818: an unreachable departure target is a priced shortfall --------------

_DEP_N, _DEP_H, _EV_KW = 12, 0.5, 12.0
_DEP_EFF = 0.95**0.5


def _ev(deadline, target, initial=3.0, **extra):
    """#1818's EV: 60 kWh, 5% SoC below a 20% floor, 12 kW."""
    return BatteryConfig(
        name="Test EV",
        capacity_kwh=60.0,
        initial_soc_kwh=initial,
        min_soc_kwh=12.0,
        max_soc_kwh=60.0,
        max_charge_kw=_EV_KW,
        max_discharge_kw=_EV_KW,
        charge_efficiency=_DEP_EFF,
        discharge_efficiency=_DEP_EFF,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.0,
        must_have_soc_by_period_index=deadline,
        must_have_soc_kwh=target,
        **extra,
    )


def _reach(periods, kw=_EV_KW, initial=3.0):
    return min(initial + periods * kw * _DEP_EFF * _DEP_H, 60.0)


def _departure_plan(ev, *, options=None, adequacy=None):
    lp._lex_calibration_failed_until = 0.0  # module state: never skip phases
    return build_plan(
        periods=PeriodGrid(
            np.full(_DEP_N, _DEP_H), start=datetime(2026, 10, 16, 18, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.full(_DEP_N, 0.30),
            export_price=np.full(_DEP_N, 0.05),
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=40.0,
                initial_soc_kwh=20.0,
                min_soc_kwh=4.0,
                max_soc_kwh=40.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.05,
            ),
            ev,
        ],
        solar=SolarConfig(forecast_kw=np.zeros(_DEP_N)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(_DEP_N))],
        adequacy_loads=adequacy,
        solve_options=options,
    )


def _ev_of(plan):
    return next(b for b in plan.batteries if b.name == "Test EV")


def test_a_departure_due_now_while_away_is_all_shortfall():
    # test_a_departure_due_now_while_away_is_all_shortfall: #1818, from 08:00: the deadline resolved to period 0 with the car
    _solver_lp._lex_calibration_failed_until = 0.0
    ev = _ev(0, 48.0, available=False, unavailable_until_period_index=2)
    plan = _departure_plan(ev, options=lp.CalibratedOptions())
    assert plan.status == "optimal"
    assert plan.departure_target_shortfall_kwh["Test EV"] == pytest.approx(45.0)

    # test_a_shared_charger_ceiling_limits_the_reach: #1818 with #467: a 12 kW car on a 7 kW shared charger reaches less
    _solver_lp._lex_calibration_failed_until = 0.0
    target = 0.5 * (_reach(2, 7.0) + _reach(2))
    ev = _ev(1, target, shared_charger_group="garage", shared_charger_max_kw=7.0)
    plan = _departure_plan(ev, options=lp.CalibratedOptions())
    assert plan.status == "optimal"
    assert "Test EV" in plan.departure_target_shortfall_kwh

    # test_a_minimum_export_commitment_is_met_and_priced
    _solver_lp._lex_calibration_failed_until = 0.0
    free = _export_site()
    assert float(np.sum(free.grid_export_kw)) * 0.5 == pytest.approx(0.0, abs=1e-6)
    bound = _export_site(min_export_kwh=2.0)
    assert bound.status == "optimal"
    assert float(np.sum(bound.grid_export_kw)) * 0.5 >= 2.0 - 1e-6
    assert bound.total_cost > free.total_cost
    assert "min_export_commitment" in bound.duals


# --- Mark Purcell's audit item 2: every objective term moves a decision ----------


def _ablation_battery(**over):
    return BatteryConfig(
        **{
            "name": "battery",
            "capacity_kwh": 20.0,
            "initial_soc_kwh": 10.0,
            "min_soc_kwh": 2.0,
            "max_soc_kwh": 20.0,
            "max_charge_kw": 10.0,
            "max_discharge_kw": 10.0,
            "charge_efficiency": 0.99,
            "discharge_efficiency": 0.99,
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "salvage_value": 0.0,
            "headroom_value": 0.0,
            **over,
        }
    )


def _empty_site(n, export, battery, **grid):
    """No load, no sun, import too dear to ever buy: the battery's only
    choice is how much of its energy to sell, so its terminal terms decide."""
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 5.0),
            export_price=np.full(n, export) if np.isscalar(export) else export,
            import_limit_kw=20.0,
            export_limit_kw=20.0,
            **grid,
        ),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="load", forecast_kw=np.zeros(n))],
    )


# --- #467 item 4: a trip deadline is bought in the cheapest hours before it -----


def _trip_plan(prices, deadline, target):
    n = len(prices)
    ev = BatteryConfig(
        name="ev",
        capacity_kwh=60.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=6.0,
        max_soc_kwh=60.0,
        max_charge_kw=7.0,
        max_discharge_kw=0.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.0,
        salvage_value=0.0,
        must_have_soc_by_period_index=None if target is None else deadline,
        must_have_soc_kwh=target,
    )
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.asarray(prices, dtype=float),
            export_price=np.full(n, 0.03),
            import_limit_kw=15.0,
            export_limit_kw=15.0,
        ),
        batteries=[ev],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="load", forecast_kw=np.full(n, 0.5))],
    )


# --- #328 / #781: soft SoC bounds recover, and their penalty is what it says -----


# --- a minimum export commitment, and limits that cannot be negative -------------------------


def _export_site(min_export_kwh=None):
    n = 4
    battery = BatteryConfig(
        name="home",
        capacity_kwh=10.0,
        initial_soc_kwh=8.0,
        min_soc_kwh=1.0,
        max_soc_kwh=10.0,
        max_charge_kw=4.0,
        max_discharge_kw=4.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.006,
        discharge_cost=0.006,
        salvage_value=0.25,
    )
    return build_plan(
        periods=PeriodGrid(np.full(n, 0.5)),
        grid=GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.02),  # exporting is not worth it on price
            import_limit_kw=10.0,
            export_limit_kw=10.0,
            min_export_kwh=min_export_kwh,
        ),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 0.5))],
    )


# Restored (#1837 follow-up): cut 1 removed these on coverage alone; a
# mutation of the code each checks survives main's suite without it. The
# export-bonus one was test_export_bonus_tiebreak.py's.
@SETTINGS
@given(
    plain_sites(),
    st.lists(_num(0.1, 1.0), min_size=1, max_size=3),
    st.lists(_num(0.0, 0.5), min_size=3, max_size=3),
    st.frozensets(st.integers(0, 23), max_size=3),
)
def test_a_tiered_terminal_value_is_counted_at_each_of_its_periods(
    site, shares, rates, extra
):
    """Terminal value tiers (v0.28.0, v0.42.0): the SoC above Min SoC at the last period
    is worth the tiers, dearest first, in place of the flat salvage; each
    extra period listed counts the same curve, split evenly between them."""
    b = site["battery"]
    span = b["max_soc_kwh"] - b["min_soc_kwh"]
    if span <= 1e-3:
        return
    widths = [span * s / sum(shares) for s in shares]
    widths[-1] = span - sum(widths[:-1])
    tiers = list(zip(widths, sorted(rates[: len(widths)], reverse=True), strict=True))
    n = len(site["hours"])
    extra = sorted(t for t in extra if t < n - 1)
    plan = _solve(
        site,
        terminal_value_breakpoints=tiers,
        terminal_value_period_indices=extra + [n - 1] if extra else None,
    )
    assert plan.status == "optimal"
    soc = plan.battery_soc_kwh
    value = _tiered_value(soc[-1] - b["min_soc_kwh"], tiers)
    for t in extra:
        value += _tiered_value(soc[t] - b["min_soc_kwh"], tiers) / len(extra)
    flat = -b["salvage_value"] * soc[-1]
    assert abs(plan.total_cost - (_parts(site, plan) - flat - value)) < 1e-6


@pytest.mark.parametrize("width", [0.25, 0.5, 1.0])
def test_a_comfort_floor_is_worth_heating_to_when_it_costs_more(width):
    """A comfort floor (`comfort_floor_c`, never above the target): each
    degree-period under it costs `comfort_floor_cost`. Heating now instead of
    at the deadline costs 0.75 of that per degree more, so a tank under the
    floor is heated to the floor now rather than later."""
    rate, start, floor, cost = 5.0, 40.0, 45.0, 1.0
    later = 0.5
    price = np.array([later + 0.75 * cost * rate, later])
    power = (floor - start) / (rate * width)
    plan = _thermal_plan(
        2,
        width,
        {
            "name": "hws",
            "max_power_kw": power * 2.0,
            "initial_temperature_c": start,
            "target_temperature_c": floor,
            "earliest_period": 0,
            "deadline_period": 1,
            "heating_rate_c_per_kwh": rate,
            "idle_decay_c_per_hour": 0.0,
            "comfort_floor_c": floor,
            "comfort_floor_cost": cost,
        },
        price=price,
    )
    assert plan.status == "optimal"
    assert abs(plan.thermal_loads[0].temperature_c[0] - floor) < 1e-5


@SETTINGS
@given(
    st.integers(3, 8),
    _num(5.0, 13.0),
    _num(0.1, 0.9),
    _num(0.05, 0.5),
    st.sampled_from(["calibrated", "lex"]),
)
def test_a_flat_export_bonus_is_claimed_in_one_block_at_the_end(
    n, rate, share, premium, mode
):
    cap = round(rate * n * share, 4)
    plan = build_plan(
        periods=PeriodGrid(
            hours=np.ones(n), start=datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.full(n, 0.15),
            export_price=np.full(n, 0.09),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
            export_bonus_price=np.full(n, premium),
            export_bonus_volume_kwh=cap,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=200.0,
                initial_soc_kwh=200.0,
                min_soc_kwh=10.0,
                max_soc_kwh=200.0,
                max_charge_kw=rate,
                max_discharge_kw=rate,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.02,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
        solve_options=lp.CalibratedOptions()
        if mode == "calibrated"
        else lp.LexOptions(),
    )
    assert plan.status == "optimal"
    np.testing.assert_allclose(plan.grid_export_kw, rate, atol=1e-4)
    bonus = plan.export_bonus_kw
    # The lexicographic phases hold the claim to their own tolerance (a few
    # watts); the calibrated solve production runs is exact.
    tol = 1e-4 if mode == "calibrated" else 2e-2
    assert abs(float(np.sum(bonus)) - cap) < 1e-4
    on = bonus > tol
    first = int(np.argmax(on))
    assert on[first:].all(), bonus  # one block, running to the end
    np.testing.assert_allclose(bonus[first + 1 :], rate, atol=tol)
