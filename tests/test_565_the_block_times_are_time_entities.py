"""P2P block start and end are time entities on the P2P sub-device, as
the reserve's are. A check that reads source text, kept apart until the
decision on source-reading tests (#1837); the block times themselves are in
tests/v2/entities/test_p2p_block_minutes.py."""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

import _solver_path  # noqa: F401

AEST = timezone(timedelta(hours=10))
_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def test_the_block_times_are_time_entities_on_the_p2p_sub_device():
    time_src = (_PKG / "time.py").read_text(encoding="utf-8")
    for n in (1, 2, 3):
        assert f'CONF_SOLVER_P2P_BLOCK_{n}_START_HOUR, "P2P Block {n} From"' in time_src
        assert f'CONF_SOLVER_P2P_BLOCK_{n}_END_HOUR, "P2P Block {n} Until"' in time_src
    assert 'f"{entry.entry_id}_p2p"' in time_src
    number_src = (_PKG / "number.py").read_text(encoding="utf-8")
    assert "CONF_SOLVER_P2P_BLOCK_1_START_HOUR" not in number_src
    sensor_src = (_PKG / "sensor.py").read_text(encoding="utf-8")
    keys = sensor_src[sensor_src.index("_SOLVER_TIME_ENTITY_KEYS = (") :]
    keys = keys[: keys.index(")")]
    assert "CONF_SOLVER_P2P_BLOCK_3_END_HOUR" in keys
