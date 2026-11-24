"""A P2P settlement history that stops advancing is a Repair, not silence.

A tester's retailer stopped publishing settled meter data for four days.
Nimbus kept planning P2P volume from the last five settled days it had and
said nothing. The history normally runs one day behind; more than three
days behind is reported, and clears once the retailer catches up.
Synthetic dates only.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sh = importlib.import_module("custom_components.nimbus_load.setup_health")
ISSUES = json.loads(
    (Path(sh.__file__).parent / "strings.json").read_text(encoding="utf-8")
)["issues"]

TODAY = date(2026, 9, 24)
SENSOR = "sensor.p2p_settlement_history"


def _history(*days):
    return {"history": {d: {"export_volume": 30.0} for d in days}}


def test_a_settlement_history_four_days_behind_is_reported():
    attrs = {**_history("2026-09-19", "2026-09-20"), "newest": "2026-09-20"}
    assert sh.settlement_history_stale(SENSOR, attrs, TODAY) == (
        SENSOR,
        "2026-09-20",
        4,
    )
    # Without a `newest` attribute the newest history day is used.
    assert sh.settlement_history_stale(
        SENSOR, _history("2026-09-19", "2026-09-20"), TODAY
    ) == (SENSOR, "2026-09-20", 4)

    issue = sh.evaluate_health(
        options={},
        forecasts=(),
        hub_up_for=sh.TRAIN_GRACE,
        settlement_stale=(SENSOR, "2026-09-20", 4),
    )
    found = [i for i in issue if i.kind == sh.KIND_SETTLEMENT_STALE]
    assert len(found) == 1
    words = set(re.findall(r"\{(\w+)\}", json.dumps(ISSUES[found[0].kind])))
    assert words == set(found[0].placeholders)


def test_a_history_up_to_three_days_behind_or_unreadable_is_not():
    for newest in ("2026-09-23", "2026-09-21"):
        assert sh.settlement_history_stale(SENSOR, _history(newest), TODAY) is None
    assert sh.settlement_history_stale(SENSOR, {}, TODAY) is None
    assert sh.settlement_history_stale(SENSOR, {"newest": "soon"}, TODAY) is None
    assert sh.settlement_history_stale(None, _history("2026-09-01"), TODAY) is None
