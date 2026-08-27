"""Data-driven invariant regression tests over captured diagnostic JSONs.

Motivation: repo issue #217 — "shift IV&V from bug-discoverer to
guardrail-validator". Each test here is an invariant the LP + price pipeline
should satisfy on any Nimbus install, expressed as a pass/fail assertion
that no future refactor can silently violate.

The initial fixture set (`fixtures/purcell_qld1/`) captures the v0.94.4
post-fix state of Mark Purcell's QLD1 install — Amber Express + Energex 6900
ToU + Sigenergy 40 kWh + both `_sensor_2` slots populated. It's the same
install and same capture that produced issue #216's fix confirmation.

To grow the suite: capture a new install into `fixtures/<name>/` (see
conftest.py's docstring for file layout), and every existing invariant runs
against it automatically via pytest parametrisation.

Naming convention for invariants — three prefixes matching the areas called
out in #217:
  RAW-*   — `_raw` diagnostic attribute conventions
  PRICE-* — price pipeline source pass-through
  LP-*    — LP output invariants (SoC, power, signs, energy balance)
"""

from __future__ import annotations

from datetime import datetime


# ─────────────────────────────────────────────────────────────
# RAW-* — `_raw` diagnostic attribute conventions (#217 item 2, #216)
# ─────────────────────────────────────────────────────────────


def test_raw_01_forecast_exposes_price_raw_attributes(forecast):
    """RAW-01: forecast[i] exposes both import_price_raw AND export_price_raw.

    Regression: v0.93.0 published `import_price_raw` only. v0.94.4 added
    `export_price_raw` for symmetry (#216).
    """
    assert forecast, "forecast[] is empty"
    first = forecast[0]
    for key in ("import_price_raw", "export_price_raw"):
        assert key in first, f"forecast[0] missing {key!r} (keys: {sorted(first)})"


def test_raw_02_forecast_exposes_source_quantities(forecast):
    """RAW-02: forecast[i] exposes load_kw and solar_kw as first-class fields."""
    assert forecast, "forecast[] is empty"
    for key in ("load_kw", "solar_kw"):
        assert key in forecast[0], f"forecast[0] missing {key!r}"


# ─────────────────────────────────────────────────────────────
# PRICE-* — source-sensor pass-through (#217 item 1, #216)
# ─────────────────────────────────────────────────────────────


def test_price_01_export_price_raw_matches_amber_express_source(
    forecast, amber_ex_feed_in
):
    """PRICE-01: within Amber Express feed-in's real coverage window,
    forecast[i].export_price_raw must equal the source sensor's forecast value
    at the same timestamp, to within 0.01 c/kWh (1e-4 $/kWh).

    This is the one-line #216 regression: before v0.94.4 the same assertion
    failed with `raw ≈ 0.502·src + 4.36 c/kWh` when a secondary export price
    sensor was configured; after the coverage-aware blending fix it passes.
    """
    fi_map = {
        datetime.fromisoformat(f["time"]): f["value"]
        for f in amber_ex_feed_in["attributes"]["forecast"]
    }

    aligned = 0
    for x in forecast:
        t = datetime.fromisoformat(x["time"])
        if t not in fi_map:
            continue
        aligned += 1
        raw = x.get("export_price_raw")
        assert raw is not None, f"export_price_raw missing at {t}"
        src = fi_map[t]
        assert abs(src - raw) < 1e-4, (
            f"at {t}: source={src} raw={raw} diff={raw - src:+.6f}"
        )

    assert aligned > 0, (
        "no forecast[] timestamps aligned with amber_ex_feed_in; capture window "
        "may not overlap the plan horizon"
    )


# ─────────────────────────────────────────────────────────────
# LP-* — LP output invariants (#217 item 1)
# ─────────────────────────────────────────────────────────────


def test_lp_01_soc_bounds_respected(forecast, solver_config):
    """LP-01: All forecast[i].soc_pct within [min_soc_pct, max_soc_pct]."""
    lo = float(solver_config["solver_battery_min_soc_percent"])
    hi = float(solver_config["solver_battery_max_soc_percent"])
    for x in forecast:
        soc = x["soc_pct"]
        assert lo - 0.01 <= soc <= hi + 0.01, (
            f"soc_pct {soc} out of [{lo}, {hi}] at t={x['time']}"
        )


def test_lp_02_battery_power_bounds_respected(forecast, solver_config):
    """LP-02: |forecast[i].battery_kw| within configured charge/discharge limits.

    Recalls issue #125 (discharge clamped at 1.93 kW despite 24 kW configured).
    Sign convention: + = discharge, − = charge.
    """
    max_chrg = float(solver_config["solver_max_charge_kw"])
    max_dchg = float(solver_config["solver_max_discharge_kw"])
    for x in forecast:
        kw = x["battery_kw"]
        assert kw <= max_dchg + 0.01, (
            f"discharge {kw} > max_discharge {max_dchg} at t={x['time']}"
        )
        assert -kw <= max_chrg + 0.01, (
            f"charge {-kw} > max_charge {max_chrg} at t={x['time']}"
        )


def test_lp_03_sign_conventions(forecast):
    """LP-03: grid_import_kw, grid_export_kw, solar_kw, load_kw are all
    non-negative (they are magnitudes; direction is implicit in the field name).
    """
    for x in forecast:
        for key in ("grid_import_kw", "grid_export_kw", "solar_kw", "load_kw"):
            assert x[key] >= -1e-3, f"{key}={x[key]} at t={x['time']}"


def test_lp_04_battery_energy_balance_closes_when_after_efficiency_available(
    forecast, solver_config
):
    """LP-04: Δenergy inferred from Δsoc must match Σ(battery_kw × hours),
    within 5% (recalls issue #149).

    NOTE: this test requires forecast[i].battery_kw_after_efficiency to be
    published. Without it, `battery_kw` is the LP's pre-efficiency decision
    variable and cannot reconcile against soc_pct without knowing the
    efficiency curve — the test is skipped rather than run at a loose
    tolerance that would hide real regressions.

    See #217 item 1 (LP-03 INFO in the first-cut IV&V) — Mark to file a small
    standalone issue proposing the extra attribute.
    """
    if "battery_kw_after_efficiency" not in forecast[0]:
        import pytest

        pytest.skip(
            "forecast[i].battery_kw_after_efficiency not published; energy "
            "balance cannot be closed without knowing the efficiency curve"
        )

    cap = float(solver_config["solver_battery_capacity_kwh"])
    soc0 = forecast[0]["soc_pct"]
    socN = forecast[-1]["soc_pct"]
    e_via_soc = (soc0 - socN) / 100.0 * cap
    e_via_kw = sum(x["battery_kw_after_efficiency"] * x["hours"] for x in forecast)
    rel_err = abs(e_via_kw - e_via_soc) / max(abs(e_via_soc), 1.0)
    assert rel_err < 0.05, (
        f"energy balance not closed: e_via_soc={e_via_soc:.2f} kWh, "
        f"e_via_kw={e_via_kw:.2f} kWh, rel_err={rel_err:.3f}"
    )
