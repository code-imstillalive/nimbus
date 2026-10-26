"""Nimbus writes no dashboard (HACS review, hacs/default#12099, Frenck):
"An integration should not write the user's Home Assistant configuration on
its own, and never into objects it did not create." The household, 26 Oct
2026: "definitely do what Frenck wants".

Checked against the source, so a future change that brings any dashboard
write back fails here, whichever module it lands in.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"

# Every way an integration could write a dashboard: Lovelace's private
# dashboards collection (reached through the websocket handler), a dashboard
# config save, or the lovelace websocket commands that write.
FORBIDDEN = (
    r"async_create_item",
    r"lovelace/dashboards/create",
    r"lovelace/config/save",
    r"\.async_save\(new_config",
    r"hass\.data\.get\(\"websocket_api\"\)",
    r"async_ensure_nimbus_dashboard",
)


def test_no_module_writes_a_dashboard():
    # test_no_module_writes_a_dashboard
    hits = []
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if re.search(pattern, text):
                hits.append(f"{path.name}: {pattern}")
    assert hits == []

    # test_setup_does_not_touch_dashboards
    init = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    assert "forecaster_dashboard" not in init.replace("forecaster_dashboard.py", "")

    # test_the_dashboard_is_returned_on_request
    import yaml

    services = yaml.safe_load((PACKAGE / "services.yaml").read_text(encoding="utf-8"))
    assert "get_dashboard" in services
