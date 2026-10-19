"""#1786 (Mark): the AEMO price source priced by contract.

A LocalVolts site's spot is RRP x MLF x DLF (x GST on import). With the
site's TNI and DLF code configured, the AEMO-based source becomes `aemo_rrp`:
the factored RRP plus only the retailer's learned fees, the current interval
from the dispatch price. Without codes, nothing changes (`aemo_markup`).
Synthetic values only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import pytest
import solver_writer
from solver_inputs import loss_factors, price_consolidation, prices

T0 = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)  # 20:00 NEM time
GRID = [T0 + timedelta(minutes=5 * i) for i in range(6)]
K = 0.9912 * 1.0594  # NCMA x BL0A, 2026-27


def _bucket(t):
    return t.hour * 12 + t.minute // 5


def test_the_series_is_factored_rrp_plus_fees():
    pts = [(T0, 0.10), (T0 + timedelta(minutes=15), 0.30)]
    fees = {_bucket(t): 0.02 for t in GRID}
    out = price_consolidation.aemo_rrp_series(GRID, pts, fees, _bucket, lambda t: K)
    assert out[0] == pytest.approx(0.10 * K + 0.02)
    assert out[3] == pytest.approx(0.30 * K + 0.02)


def test_the_current_interval_is_the_dispatch_price_not_the_forecast():
    pts = [(T0, 0.10)]
    out = price_consolidation.aemo_rrp_series(
        GRID, pts, {}, _bucket, lambda t: K, rrp_now=0.08
    )
    assert out[0] == pytest.approx(0.08 * K)
    assert out[1] == pytest.approx(0.10 * K)


def test_no_factor_or_no_aemo_point_is_none_never_a_guess():
    pts = [(T0 + timedelta(minutes=10), 0.10)]
    out = price_consolidation.aemo_rrp_series(GRID, pts, {}, _bucket, lambda t: K)
    assert out[:2] == [None, None]
    none_k = price_consolidation.aemo_rrp_series(GRID, pts, {}, _bucket, lambda t: None)
    assert none_k == [None] * len(GRID)
    assert price_consolidation.aemo_rrp_series(
        GRID, None, {}, _bucket, lambda t: K
    ) == [None] * len(GRID)


def _history(values, start=T0 - timedelta(days=1)):
    return [
        (start + timedelta(minutes=5 * i, seconds=20), v) for i, v in enumerate(values)
    ]


def test_fees_are_what_is_left_after_the_contract_scaling(monkeypatch):
    """Retail = RRP x K + fee at every price level: with the scale, the
    learned offset is exactly the fee; without it, it absorbs (K - 1) x RRP
    and moves with the price."""
    rrp = [0.05, 0.10, 0.50, 3.00] * 3
    fee = 0.02
    retail = [r * K + fee for r in rrp]
    wholesale = {int(t.timestamp()) // 300: (t, v) for t, v in _history(rrp)}
    monkeypatch.setattr(
        solver_writer, "_wholesale_by_interval", lambda *a, **k: dict(wholesale)
    )
    scaled = solver_writer.compute_5min_offset(
        _history(retail), regional_spot_sensor="x", wholesale_scale=lambda t: K
    )
    assert scaled and all(v == pytest.approx(fee) for v in scaled.values())
    plain = solver_writer.compute_5min_offset(
        _history(retail), regional_spot_sensor="x"
    )
    spread = max(plain.values()) - min(plain.values())
    assert spread > 0.1  # (K - 1) x 2.95 $/kWh across the price levels


class _FakeSW:
    def __init__(self, states, rrp_now=0.08):
        self._states = states
        self._rrp_now = rrp_now
        self.scales = []

    def ha_get(self, entity_id):
        return {"state": self._states[entity_id]}

    def compute_5min_offset(self, history, **kwargs):
        self.scales.append(kwargs["wholesale_scale"])
        return {}

    def price_now(self, entity_id, fallback=0.0):
        return self._rrp_now


CFG = {
    "solver_loss_factor_tni_sensor": "sensor.tni",
    "solver_loss_factor_dlf_code_sensor": "sensor.dlf",
    "solver_regional_spot_current_price_sensor": "sensor.rrp_now",
}


def test_the_contract_path_prices_import_with_gst_and_reports_the_factor():
    sw = _FakeSW({"sensor.tni": "NCMA", "sensor.dlf": "BL0A"})
    imp, exp, summary = prices._aemo_rrp_by_contract(
        sw, CFG, T0, GRID, [(T0, 0.10)], [], [], {}
    )
    assert summary == {
        "tni": "NCMA",
        "dlf_code": "BL0A",
        "financial_year": "2026-27",
        "value": round(K, 6),
        "reason": "ok",
    }
    assert exp[0] == pytest.approx(0.08 * K)
    assert imp[0] == pytest.approx(0.08 * K * loss_factors.IMPORT_GST)
    assert imp[1] == pytest.approx(0.10 * K * loss_factors.IMPORT_GST)
    import_scale, export_scale = sw.scales
    assert import_scale(T0) == pytest.approx(K * loss_factors.IMPORT_GST)
    assert export_scale(T0) == pytest.approx(K)


def test_no_codes_leaves_the_source_as_it_was():
    sw = _FakeSW({})
    cfg = {"solver_regional_spot_current_price_sensor": "sensor.rrp_now"}
    assert (
        prices._aemo_rrp_by_contract(sw, cfg, T0, GRID, [(T0, 0.1)], [], [], {}) is None
    )


def test_codes_that_cannot_be_looked_up_say_why_and_price_nothing():
    sw = _FakeSW({"sensor.tni": "ZZZZ", "sensor.dlf": "unavailable"})
    imp, exp, summary = prices._aemo_rrp_by_contract(
        sw, CFG, T0, GRID, [(T0, 0.1)], [], [], {}
    )
    assert (imp, exp) == (None, None)
    assert summary["reason"] == "no_codes"
    sw = _FakeSW({"sensor.tni": "ZZZZ", "sensor.dlf": "BL0A"})
    _, _, summary = prices._aemo_rrp_by_contract(sw, CFG, T0, GRID, [], [], [], {})
    assert summary["reason"] == "unknown_tni"


def test_the_consolidation_names_the_source_by_how_it_was_priced():
    class P:
        import_real_mask = [True] * len(GRID)
        aemo_import_series = [0.1] * len(GRID)
        aemo_source = "aemo_rrp"

    out = solver_writer._consolidation_sources({}, GRID, "import", [0.2] * len(GRID), P)
    assert "aemo_rrp" in out and "aemo_markup" not in out
    P.aemo_source = "aemo_markup"
    out = solver_writer._consolidation_sources({}, GRID, "import", [0.2] * len(GRID), P)
    assert "aemo_markup" in out
