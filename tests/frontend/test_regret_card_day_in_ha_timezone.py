"""The Regret card asks for its day in Home Assistant's own time zone.

The card built its window as `<day>T00:00:00+10:00`, Brisbane's offset,
written in when the reference household was the only install. Any install
in another zone, or on daylight saving, was scored on Brisbane's midnight.
`compute_quality_report` already anchors an offset-less time to Home
Assistant's configured zone (`services._coerce_datetime`), so the card
sends local midnight with no offset at all.
"""

from __future__ import annotations

import pathlib
import re

CARD = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-regret-card.js"
)


def test_the_scored_day_carries_no_fixed_utc_offset():
    code = "\n".join(
        line
        for line in CARD.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("//")
    )
    assert not re.search(r"T00:00:00[+-]\d\d:\d\d", code), (
        "a fixed offset scores every other zone on the wrong midnight"
    )
    assert "T00:00:00`" in code
