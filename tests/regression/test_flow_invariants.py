"""FLOW-* — seven-flow decomposition invariants over captured diagnostic
JSONs (nimbus issue #264, Mark Purcell).

Runs the REAL `_flow_decomposition()` function (imported directly from
`solver_writer.py`, not reimplemented) against every real captured
`forecast[]` row across every fixture under `fixtures/`, the same
data-driven pattern as `test_forecast_invariants.py`'s LP-* suite.

Every captured fixture publishes netted `battery_kw` only (none predate
the separate charge/discharge fields this function can otherwise take
advantage of for same-period wash-trade visibility -- see
_flow_decomposition()'s own docstring) -- charge_kw/discharge_kw are
reconstructed here via max(0, -battery_kw)/max(0, battery_kw), the same
netted convention _dispatch_source_breakdown() itself already uses and
that every existing fixture was captured against.

FLOW-01/02/03/04 assert the four invariants that hold by construction,
always (see _flow_decomposition()'s own docstring) -- a genuine
regression here would mean the function itself was changed incorrectly.

FLOW-05/06 assert the two invariants that are NOT algebraic identities
of the function alone -- that the merit-order reconstruction of
grid_import_kw/grid_export_kw agrees with what the real LP actually
published for that row. These are the ones capable of catching a real
merit-order-assumption mismatch against a real install's actual LP
behaviour (e.g. simultaneous import+export in some row).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _solver_path  # noqa: F401
import solver_writer

_flow_decomposition = solver_writer._flow_decomposition

# Reconciliation tolerance for FLOW-05/06 -- the merit-order
# reconstruction can differ from the real LP's own published grid_import/
# grid_export by a few hundredths of a kW on rows with rounding at the
# edge of a period boundary or (rare) simultaneous import+export; a
# genuine merit-order violation would be off by much more than this.
_RECON_TOL_KW = 0.5


def _skips_apply(fixture_skips: set[str], *tokens: str) -> bool:
    """Same convention as test_forecast_invariants.py's own helper: a
    fixture that lists a broad prefix (``FLOW``) opts out of every
    sub-invariant under it; one that lists a specific token
    (``FLOW_05``) opts out of just that one."""
    return any(t in fixture_skips for t in tokens)


def test_flow_01_pv_flows_sum_to_solar_kw(forecast):
    for row in forecast:
        charge_kw = max(0.0, -row["battery_kw"])
        discharge_kw = max(0.0, row["battery_kw"])
        flow = _flow_decomposition(
            row["solar_kw"], row["load_kw"], charge_kw, discharge_kw
        )
        total = flow["pv_to_load"] + flow["pv_to_battery"] + flow["pv_to_grid"]
        assert abs(total - row["solar_kw"]) < 1e-6, (
            f"at t={row['time']}: pv flows sum to {total}, solar_kw={row['solar_kw']}"
        )


def test_flow_02_load_flows_sum_to_load_kw(forecast):
    for row in forecast:
        charge_kw = max(0.0, -row["battery_kw"])
        discharge_kw = max(0.0, row["battery_kw"])
        flow = _flow_decomposition(
            row["solar_kw"], row["load_kw"], charge_kw, discharge_kw
        )
        total = flow["pv_to_load"] + flow["battery_to_load"] + flow["grid_to_load"]
        assert abs(total - row["load_kw"]) < 1e-6, (
            f"at t={row['time']}: load flows sum to {total}, load_kw={row['load_kw']}"
        )


def test_flow_03_battery_charge_flows_sum_to_charge_kw(forecast):
    for row in forecast:
        charge_kw = max(0.0, -row["battery_kw"])
        discharge_kw = max(0.0, row["battery_kw"])
        flow = _flow_decomposition(
            row["solar_kw"], row["load_kw"], charge_kw, discharge_kw
        )
        total = flow["pv_to_battery"] + flow["grid_to_battery"]
        assert abs(total - charge_kw) < 1e-6, (
            f"at t={row['time']}: charge flows sum to {total}, charge_kw={charge_kw}"
        )


def test_flow_04_battery_discharge_flows_sum_to_discharge_kw(forecast):
    for row in forecast:
        charge_kw = max(0.0, -row["battery_kw"])
        discharge_kw = max(0.0, row["battery_kw"])
        flow = _flow_decomposition(
            row["solar_kw"], row["load_kw"], charge_kw, discharge_kw
        )
        total = flow["battery_to_load"] + flow["battery_to_grid"]
        assert abs(total - discharge_kw) < 1e-6, (
            f"at t={row['time']}: discharge flows sum to {total}, "
            f"discharge_kw={discharge_kw}"
        )


def test_flow_05_grid_flows_reconcile_with_real_grid_import(forecast, fixture_skips):
    if _skips_apply(fixture_skips, "FLOW", "FLOW_05"):
        import pytest

        pytest.skip("fixture opts out of FLOW-05 (see SKIP_INVARIANTS.txt)")
    failures = []
    for row in forecast:
        charge_kw = max(0.0, -row["battery_kw"])
        discharge_kw = max(0.0, row["battery_kw"])
        flow = _flow_decomposition(
            row["solar_kw"], row["load_kw"], charge_kw, discharge_kw
        )
        recon = flow["grid_to_load"] + flow["grid_to_battery"]
        real = row["grid_import_kw"]
        if abs(recon - real) >= _RECON_TOL_KW:
            failures.append(f"t={row['time']}: recon={recon:.3f} real={real:.3f}")
    assert not failures, (
        f"FLOW-05: {len(failures)}/{len(forecast)} row(s) failed grid_import "
        f"reconciliation beyond {_RECON_TOL_KW}kW:\n  " + "\n  ".join(failures[:10])
    )


def test_flow_06_grid_flows_reconcile_with_real_grid_export(forecast, fixture_skips):
    if _skips_apply(fixture_skips, "FLOW", "FLOW_06"):
        import pytest

        pytest.skip("fixture opts out of FLOW-06 (see SKIP_INVARIANTS.txt)")
    failures = []
    for row in forecast:
        charge_kw = max(0.0, -row["battery_kw"])
        discharge_kw = max(0.0, row["battery_kw"])
        flow = _flow_decomposition(
            row["solar_kw"], row["load_kw"], charge_kw, discharge_kw
        )
        recon = flow["pv_to_grid"] + flow["battery_to_grid"]
        real = row["grid_export_kw"]
        if abs(recon - real) >= _RECON_TOL_KW:
            failures.append(f"t={row['time']}: recon={recon:.3f} real={real:.3f}")
    assert not failures, (
        f"FLOW-06: {len(failures)}/{len(forecast)} row(s) failed grid_export "
        f"reconciliation beyond {_RECON_TOL_KW}kW:\n  " + "\n  ".join(failures[:10])
    )
