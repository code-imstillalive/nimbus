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

Naming convention for invariants — four prefixes matching the areas called
out in #217:
  RAW-*   — `_raw` diagnostic attribute conventions
  PRICE-* — price pipeline source pass-through
  SET-*   — settled-block price identity (#220)
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
# SET-* — settled current-block prices are not blended (#220)
# ─────────────────────────────────────────────────────────────
#
# Settled prices on the NEM (and, transitively, Amber and Local Volts) are
# the closed value for the current 5-minute dispatch interval. They are a
# fact, not a forecast — nothing Nimbus does downstream can change them,
# and blending them with a secondary forecast produces an artefact that
# doesn't correspond to any real market price. The invariant this section
# codifies is: at the current settlement block (forecast[0], by LP
# convention), the published price MUST equal the primary source sensor's
# live `state`, and it MUST equal its own `_raw` companion (i.e. the blend
# is bypassed for this row).
#
# Regression: v0.94.4 blended the current block against the forecast[] of
# a secondary sensor and *also* published a stale sample as `_raw`. See
# issue #220 for the numeric evidence. v0.94.5 (Raf, 2026-08-27) fixed
# both — the writer now reads primary_sensor.state directly for the
# current row and applies it after any downstream blending.
#
# Tolerance discussion: Nimbus rounds published forecast prices to 4 dp
# ($/kWh), while `state` may carry more precision (e.g. state=0.0318708,
# forecast=0.0319). The tolerance is set at 0.05 c/kWh (5e-4 $/kWh) — half
# a rounding step — which comfortably distinguishes "identity, modulo
# rounding" from "blended value" (which differed by 0.5–1.5 c/kWh in
# the #220 evidence).


def _current_block_row(forecast):
    """The forecast row that represents the current settlement block. By LP
    convention this is forecast[0]: the plan starts at "now" and each row
    covers the next 5-minute interval forward."""
    assert forecast, "forecast[] is empty"
    return forecast[0]


def _skips_apply(fixture_skips: set[str], *tokens: str) -> bool:
    """True if any of `tokens` is present in the fixture's opt-out set.

    A fixture that lists a broad prefix (e.g. ``SET``) opts out of every
    sub-invariant under that prefix (``SET_01``, ``SET_02``, …). A fixture
    that lists a specific token (e.g. ``SET_02``) opts out of just that one.
    """
    return any(t in fixture_skips for t in tokens)


def test_set_01_current_block_import_matches_source_state(
    forecast, amber_ex_general, fixture_skips
):
    """SET-01a: at the current settlement block, forecast[0].import_price and
    forecast[0].import_price_raw both equal the primary import sensor's live
    `state`, to within 0.05 c/kWh (one rounding-step tolerance).

    This is the #220 identity for the import side: the settled block is
    published unblended (import_price == import_price_raw) and its value
    matches the source sensor's `state` (not a stale sample, not a blend).

    Before v0.94.5 this failed with import_price ≈ blend of
    (primary_state, secondary_forecast_at_t0), and import_price_raw was a
    stale sample from the primary sensor's forecast[] array rather than
    its live state.
    """
    if _skips_apply(fixture_skips, "SET", "SET_01"):
        import pytest

        pytest.skip("fixture opts out of SET-01 (see SKIP_INVARIANTS.txt)")
    row = _current_block_row(forecast)
    src_state = float(amber_ex_general["state"])
    ip = row["import_price"]
    ipr = row["import_price_raw"]

    # Identity 1: settled block equals the source's live state (modulo rounding)
    assert abs(ip - src_state) < 5e-4, (
        f"at t0={row['time']}: import_price={ip} vs source_state={src_state} "
        f"(diff={ip - src_state:+.6f}); a blended value would differ by ≫ 5e-4"
    )
    # Identity 2: blend is bypassed for the current row → published == _raw
    assert abs(ip - ipr) < 1e-6, (
        f"at t0={row['time']}: import_price={ip} != import_price_raw={ipr} "
        "(current block must not be blended; #220)"
    )


def test_set_01_current_block_export_matches_source_state(
    forecast, amber_ex_feed_in, fixture_skips
):
    """SET-01b: at the current settlement block, forecast[0].export_price and
    forecast[0].export_price_raw both equal the primary export sensor's live
    `state`, to within 0.05 c/kWh (one rounding-step tolerance).

    Same #220 identity as SET-01a but on the export side. Getting this right
    matters most when Amber's feed-in price is *negative* (a genuine market
    signal that the grid is oversupplied) — under the old blend a negative
    settled export could be diluted to positive by the secondary forecast,
    causing the LP to export into a curtailment window.
    """
    if _skips_apply(fixture_skips, "SET", "SET_01"):
        import pytest

        pytest.skip("fixture opts out of SET-01 (see SKIP_INVARIANTS.txt)")
    row = _current_block_row(forecast)
    src_state = float(amber_ex_feed_in["state"])
    ep = row["export_price"]
    epr = row["export_price_raw"]

    assert abs(ep - src_state) < 5e-4, (
        f"at t0={row['time']}: export_price={ep} vs source_state={src_state} "
        f"(diff={ep - src_state:+.6f}); a blended value would differ by ≫ 5e-4"
    )
    assert abs(ep - epr) < 1e-6, (
        f"at t0={row['time']}: export_price={ep} != export_price_raw={epr} "
        "(current block must not be blended; #220)"
    )


# ─────────────────────────────────────────────────────────────
# SET-02 — settled-block identity holds for *every* row in the
# current NEM 5-minute block, not just forecast[0] (#220 reopen → #234)
# ─────────────────────────────────────────────────────────────
#
# NEM settlement is a 5-minute block. Within that block the settled price is
# a single scalar fact — every row Nimbus publishes whose timestamp falls in
# [block_start, block_start + 5 min) must reflect that same scalar, unblended.
#
# The original #220 fix landed the identity guarantee on forecast[0] (the
# leading "align-to-now" row). Live evidence from Mark Purcell's install on
# 27-Aug-2026 showed that forecast[1..N] — the 60 s "tier-0 stretch" rows
# `build_tiered_grid()` emits from now up to the next clean 5-minute mark —
# retain the blended value. `import_price_raw` and `export_price_raw` DO carry
# the correct identity value on those rows (proving the source data is
# available in the writer's local scope), so the fix in PR #234 extends the
# row-0-only override to all in-block rows.
#
# The v0.94.6 fixture (`purcell_qld1_v0.94.6/`) happens to be captured at
# 15:05:00 — a NEM boundary — so forecast[0] is the only in-block row and
# these tests will trivially pass with a single-row assertion. The
# `purcell_qld1_v0.94.6_midblock/` fixture is captured mid-block on purpose
# to exercise the multi-row case that PR #225's original SET-01 missed.


def _nem_block_start(row_time_iso: str):
    """Floor an ISO-8601 row timestamp to its NEM 5-minute block start.

    Returns a timezone-aware datetime aligned to :00, :05, :10, …, :55.
    """
    t = datetime.fromisoformat(row_time_iso)
    return t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)


def _rows_in_current_nem_block(forecast: list[dict]) -> list[dict]:
    """All forecast rows whose timestamp is inside the NEM 5-minute block
    that contains forecast[0]. Always contains at least forecast[0].

    This does NOT include the first row of the next block. Boundary rows
    (e.g. 17:35 when block starts at 17:30) belong to the *next* block.
    """
    assert forecast, "forecast[] is empty"
    from datetime import timedelta

    block_start = _nem_block_start(forecast[0]["time"])
    block_end = block_start + timedelta(minutes=5)
    rows: list[dict] = []
    for r in forecast:
        t = datetime.fromisoformat(r["time"])
        if block_start <= t < block_end:
            rows.append(r)
        elif t >= block_end:
            break
    return rows


def test_set_02_all_in_block_rows_import_matches_source_state(
    forecast, amber_ex_general, fixture_skips
):
    """SET-02a: for EVERY forecast row inside the current NEM 5-minute block,
    import_price == import_price_raw == amber_general.state (to 0.05 c/kWh).

    Regression: #220 reopen (27-Aug-2026), fixed in PR #234. The row-0-only
    fix from v0.94.5 left rows 1..N inside the current NEM block still blended.
    Their `import_price_raw` fields already carried the correct identity value
    (proving the source data was available in the writer's local scope), but
    the effective `import_price` — the field the LP actually consumes for the
    objective — was the pre-fix blended value.

    Fixtures captured on a NEM boundary (e.g. purcell_qld1_v0.94.6/, at
    15:05:00) trivially pass this test with a single in-block row. Fixtures
    captured mid-block exercise the multi-row case.
    """
    if _skips_apply(fixture_skips, "SET", "SET_02"):
        import pytest

        pytest.skip("fixture opts out of SET-02 (see SKIP_INVARIANTS.txt)")
    src_state = float(amber_ex_general["state"])
    in_block = _rows_in_current_nem_block(forecast)
    assert in_block, "no forecast rows in current NEM block (unexpected)"

    failures = []
    for r in in_block:
        ip = r["import_price"]
        ipr = r["import_price_raw"]
        # Identity to source state (modulo rounding)
        if abs(ip - src_state) >= 5e-4:
            failures.append(
                f"at t={r['time']}: import_price={ip} vs source_state={src_state} "
                f"(diff={ip - src_state:+.6f})"
            )
        # Identity to _raw (blend must be bypassed for every in-block row)
        if abs(ip - ipr) >= 1e-6:
            failures.append(
                f"at t={r['time']}: import_price={ip} != import_price_raw={ipr} "
                "(in-block row is blended; #220 reopen)"
            )
    assert not failures, (
        f"SET-02a: {len(failures)} in-block row(s) failed identity (of "
        f"{len(in_block)} rows in block starting "
        f"{_nem_block_start(forecast[0]['time'])}):\n  " + "\n  ".join(failures)
    )


def test_set_02_all_in_block_rows_export_matches_source_state(
    forecast, amber_ex_feed_in, fixture_skips
):
    """SET-02b: for EVERY forecast row inside the current NEM 5-minute block,
    export_price == export_price_raw == amber_feed_in.state (to 0.05 c/kWh).

    Export-side companion to SET-02a. Getting this right matters most when
    the settled block feed-in price is a spike (or negative). A blended
    non-row-0 value dampens the price signal the LP sees for the remaining
    seconds of the current NEM block, delaying dispatch response.
    """
    if _skips_apply(fixture_skips, "SET", "SET_02"):
        import pytest

        pytest.skip("fixture opts out of SET-02 (see SKIP_INVARIANTS.txt)")
    src_state = float(amber_ex_feed_in["state"])
    in_block = _rows_in_current_nem_block(forecast)
    assert in_block, "no forecast rows in current NEM block (unexpected)"

    failures = []
    for r in in_block:
        ep = r["export_price"]
        epr = r["export_price_raw"]
        if abs(ep - src_state) >= 5e-4:
            failures.append(
                f"at t={r['time']}: export_price={ep} vs source_state={src_state} "
                f"(diff={ep - src_state:+.6f})"
            )
        if abs(ep - epr) >= 1e-6:
            failures.append(
                f"at t={r['time']}: export_price={ep} != export_price_raw={epr} "
                "(in-block row is blended; #220 reopen)"
            )
    assert not failures, (
        f"SET-02b: {len(failures)} in-block row(s) failed identity (of "
        f"{len(in_block)} rows in block starting "
        f"{_nem_block_start(forecast[0]['time'])}):\n  " + "\n  ".join(failures)
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
    published (shipped in the writer alongside this test change). Without it,
    `battery_kw` is the LP's pre-efficiency decision variable and cannot
    reconcile against soc_pct without knowing the efficiency curve — the
    test is skipped rather than run at a loose tolerance that would hide
    real regressions.

    Fixtures captured before the field shipped will continue to skip cleanly;
    the next re-capture on an install running post-#229 code will start
    exercising the invariant.
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
