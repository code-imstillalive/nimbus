"""nimbus #1825: the regret card has a sentence for a stale-price day. A
front-end check (#1837 group C, kept); the re-pricing itself is in
tests/v2/reports/test_quality_window.py."""

from __future__ import annotations

import sys
from pathlib import Path

import _solver_path  # noqa: F401
import solver_writer as sw

quality = sys.modules[sw.publish_daily_quality_report.__module__]


def test_the_card_has_a_sentence_for_a_stale_price_day():
    card_path = (
        Path(sw.__file__).resolve().parent / "frontend" / "nimbus-regret-card.js"
    )
    card = card_path.read_text(encoding="utf-8")
    assert f'code === "{sw._RELIABILITY_PRICE}"' in card
    assert 'reason === "price_source_stale"' in card
    assert "attrs.price_stale_repriced === true" in card
