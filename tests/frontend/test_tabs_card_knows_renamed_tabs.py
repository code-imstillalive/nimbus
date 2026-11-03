"""Household, 3 Nov 2026: after the Tabs card added "Topology (new)" and
"Help (new)", the household kept the new ones, deleted their old tabs and
renamed the copies back to "Topology" and "Help". The copies keep their path
("topology-new", "help-new"), so the card matched by path alone would have
called those tabs missing and added them a second time. Their Forecaster tab
has no path at all, and was added again as a second "Forecaster".

The card now treats the tab at "<path>-new" (no longer titled "(new)") and a
path-less tab under the standard title as the household's own, compares
content without the name and path, and puts a newer version at the free path.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

CARD = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-tabs-card.js"
)

SCRIPT = """
const views = %s, standard = %s;
const out = {};
for (const t of standard) {
  const r = nimbusTabsFind(views, t);
  out[t.path] = { ...r, same: r.mine >= 0 && nimbusTabsSame(views[r.mine], t) };
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_renamed_and_pathless_tabs_are_the_households_own():
    src = CARD.read_text(encoding="utf-8").split("class NimbusTabsCard")[0]
    standard = [
        {"title": "Topology", "path": "topology", "cards": [1]},
        {"title": "Help", "path": "help", "cards": [2]},
        {"title": "Forecaster", "path": "forecaster", "cards": [3]},
        {"title": "Regret", "path": "regret", "cards": [4]},
        {"title": "Solver", "path": "solver", "cards": [5]},
    ]
    views = [
        {"title": "Forecaster", "cards": [3]},
        {"title": "Topology", "path": "topology-new", "cards": [1]},
        {"title": "Help", "path": "help-new", "cards": [9]},
        {"title": "Regret", "path": "regret", "cards": [4]},
        {"title": "Solver", "path": "solver", "cards": [6]},
        {"title": "Solver (new)", "path": "solver-new", "cards": [5]},
    ]
    res = subprocess.run(
        ["node", "-e", src + SCRIPT % (json.dumps(views), json.dumps(standard))],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    got = json.loads(res.stdout)

    # A kept "(new)" copy, renamed back: theirs, up to date, offered at the base path.
    assert got["topology"] == {
        "mine": 1,
        "offered": -1,
        "offerPath": "topology",
        "same": True,
    }
    # Theirs but older: the new version goes at the free base path, not "help-new".
    assert got["help"] == {"mine": 2, "offered": -1, "offerPath": "help", "same": False}
    # No path, standard title: theirs, so no second Forecaster.
    assert got["forecaster"]["mine"] == 0 and got["forecaster"]["same"] is True
    assert got["regret"]["mine"] == 3 and got["regret"]["same"] is True
    # A "(new)" copy still titled so is the card's proposal, not theirs.
    assert got["solver"] == {
        "mine": 4,
        "offered": 5,
        "offerPath": "solver-new",
        "same": False,
    }
