#!/usr/bin/env python3
"""Generates the "Topology" dashboard view from config/integrations/topology_map.yaml
-- the real switchboard wiring skeleton (Switchboard hub, 2 inverters with their own PV
strings + battery towers, 18 loads, whole-house comparison).

This is a GENERATOR, not a hand-edited card set, on purpose: "plugging in" a new element
(a load, a PV string, a battery tower) means adding one entry to topology_map.yaml and
re-running this script -- never hand-editing dashboard cards for this view directly. Same
established project pattern as every other lovelace_*.py script here (never hand-edit the
live storage file with a text editor, never use PowerShell Out-File on it -- BOM corruption
history documented at length in CLAUDE.md).

Idempotent: removes any existing "topology" view before regenerating, so re-running after
editing topology_map.yaml cleanly replaces the whole view rather than duplicating it.

Run inside the HA container:
  docker exec opt_homeassistant_1 python3 /tmp/lovelace_build_topology_dashboard.py
Then:
  docker restart opt_homeassistant_1
"""
import json

import yaml

LOVELACE_PATH = "/config/.storage/lovelace.dashboard_nimbus"
# Deliberately NOT under /config/integrations/ -- that directory is
# auto-loaded as HA packages (!include_dir_named integrations, per this
# project's own architecture). A data file with unrecognized top-level
# keys (switchboard:, inverters:, etc.) sitting there risks a config
# validation error blocking HA from starting. Plain /config/ root is safe
# -- HA only loads what configuration.yaml's own include directives name,
# and nothing references this file.
TOPOLOGY_PATH = "/config/topology_map.yaml"

def load_topology():
    with open(TOPOLOGY_PATH) as f:
        return yaml.safe_load(f)


def diagram_card(topo):
    """The real visual: a custom SVG node-graph (config/www/topology-card-v4.js),
    not an entities list. Config shape mirrors topology_map.yaml exactly --
    same data, different renderer. Requires topology-card-v4.js registered as a
    dashboard resource once (see that file's own header comment).

    Carries ALL the detail (per-load live+forecast, full battery tower
    stats, whole-house readout) directly on the diagram itself -- there
    are deliberately no separate entities-list cards any more. Splitting
    the same information across a diagram AND a wall of list cards below
    it was the wrong call; one real diagram is the whole dashboard."""
    return {
        "type": "custom:switchboard-topology-card",
        "title": "",
        "switchboard": topo["switchboard"],
        "inverters": topo["inverters"],
        # No "loads" key -- v7 of topology-card-v4.js auto-discovers every
        # Nimbus LOAD subentry's forecast sensor directly from live hass
        # state on every render (see that file's own header comment), so
        # there's nothing left to hand-list here. Adding a new load in the
        # Nimbus UI is now the entire "how do I add it to the topology
        # card" step -- no re-running this generator, no restart.
        "whole_house": topo["whole_house"],
        "weather": topo.get("weather"),
        "nimbus_version": topo.get("nimbus_version"),
    }


def build_view(topo):
    # type: "panel" -- a dedicated view type that renders exactly one card
    # filling the ENTIRE page edge-to-edge, no column-width math involved
    # at all. sections' grid_options: "full" was tried twice (PR #549/#550)
    # and still rendered narrow live both times -- panel is the correct
    # tool for "exactly one big card, no other cards on this page", not a
    # sections-view workaround.
    return {
        # No "icon" key -- a view with both title and icon shows only the
        # icon in the tab strip, hiding the text (documented gotcha in this
        # project). Text title is what was actually asked for here.
        "type": "panel",
        "title": "Nimbus Topology",
        "path": "topology",
        "cards": [diagram_card(topo)],
    }


def main():
    topo = load_topology()
    new_view = build_view(topo)

    with open(LOVELACE_PATH) as f:
        data = json.load(f)

    views = data["data"]["config"]["views"]
    views[:] = [v for v in views if v.get("path") != "topology"]
    views.append(new_view)

    with open(LOVELACE_PATH, "w") as f:
        json.dump(data, f)

    print(f"Topology view generated: 1 diagram card "
          f"({len(topo['inverters'])} inverters, loads auto-discovered live).")


if __name__ == "__main__":
    main()
