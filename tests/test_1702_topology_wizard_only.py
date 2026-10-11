"""nimbus #1702 (household, 11 Oct 2026): Topology sensors are set in the wizard
only. The card ignores its own `switchboard`/`inverters` wherever the wizard
holds any Topology setting, and its editor shows the sensors read-only.

The precedence helpers are pure functions in the card's JS; they are run in
node when it is installed, and their shape is pinned from the source otherwise.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-topology-card.js"
)
SRC = CARD.read_text(encoding="utf-8")


def _helpers() -> str:
    start = SRC.index("const TOPOLOGY_SWITCHBOARD_FIELDS = {")
    end = SRC.index("// Real, deterministic inverse of Nimbus's own")
    return SRC[start:end]


def _run(expr: str):
    out = subprocess.run(
        ["node", "-e", _helpers() + f"\nconsole.log(JSON.stringify({expr}));"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


WIZARD = {
    "power_sources": [{"subentry_id": "a"}],
    "switchboard": {
        "switchboard_grid_meter_sensor": "sensor.new_grid",
        "switchboard_house_load_energy_daily_sensor": "sensor.new_house",
    },
}
CARD_CONFIG = {
    "switchboard": {
        "grid_meter": "sensor.old_grid",
        "solar_energy_daily": "sensor.old_solar",
    },
    "inverters": [{"name": "old"}],
}


def test_render_no_longer_merges_the_card_under_the_wizard() -> None:
    render = SRC[SRC.index("  _render() {") :]
    render = render[: render.index("roleSignals")]
    assert "...this._config.switchboard" not in render
    assert "topologyWizardHolds(topoAttrs)" in render
    assert "switchboard: topologyWizardSwitchboard(topoAttrs)" in render


def test_the_editor_is_read_only_and_points_at_the_wizard() -> None:
    assert "static getConfigElement()" in SRC
    assert 'nimbusRegisterCard("nimbus-topology-card-editor"' in SRC
    editor = SRC[SRC.index("class NimbusTopologyCardEditor") :]
    editor = editor[: editor.index('nimbusRegisterCard("nimbus-topology-card-editor"')]
    assert "Topology diagram settings" in editor
    # No inputs: the only control is the button that removes ignored entries.
    assert not re.search(r"<input|ha-entity-picker|ha-form", editor)
    assert "Ignored:" in editor


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_wizard_wins_whole_and_the_card_entries_are_named_ignored() -> None:
    wizard = json.dumps(WIZARD)
    card = json.dumps(CARD_CONFIG)
    sb, holds, ignored = _run(
        f"[topologyWizardSwitchboard({wizard}), topologyWizardHolds({wizard}),"
        f" topologyIgnoredEntries({card}, {wizard})]"
    )
    assert holds is True
    assert sb["grid_meter"] == "sensor.new_grid"
    assert sb["house_load_energy_daily"] == "sensor.new_house"
    # The card's stale solar entry does not fill the wizard's gap.
    assert sb["solar_energy_daily"] is None
    assert ignored == [
        "switchboard.grid_meter",
        "switchboard.solar_energy_daily",
        "inverters",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_an_install_without_the_wizard_keeps_its_own_card_config() -> None:
    card = json.dumps(CARD_CONFIG)
    holds_none, holds_empty, ignored = _run(
        "[topologyWizardHolds(null),"
        " topologyWizardHolds({power_sources: [], switchboard: {}}),"
        f" topologyIgnoredEntries({card}, null)]"
    )
    assert holds_none is False
    assert holds_empty is False
    assert ignored == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_switchboard_settings_alone_count_as_the_wizard_holding_topology() -> None:
    attrs = json.dumps(
        {
            "power_sources": [],
            "switchboard": {"switchboard_grid_meter_sensor": "sensor.g"},
        }
    )
    assert _run(f"topologyWizardHolds({attrs})") is True
