"""nimbus #1825: two checks that read source text, kept apart until the
decision on source-reading tests (#1837). The behaviour itself is in
tests/v2/reports/test_quality_window.py."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import _solver_path  # noqa: F401
import solver_writer as sw

quality = sys.modules[sw.publish_daily_quality_report.__module__]


def test_prices_are_repriced_before_anything_is_priced_on_them():
    src = inspect.getsource(quality._compute_report_for_window)
    assert src.index("_reprice_stale_price_periods(") < src.index("grid_residual = ")
    assert 'price_stale["price_stale_unusable"],\n        ),' in src


def test_the_card_has_a_sentence_for_a_stale_price_day():
    card_path = (
        Path(sw.__file__).resolve().parent / "frontend" / "nimbus-regret-card.js"
    )
    card = card_path.read_text(encoding="utf-8")
    assert f'code === "{sw._RELIABILITY_PRICE}"' in card
    assert 'reason === "price_source_stale"' in card
    assert "attrs.price_stale_repriced === true" in card
