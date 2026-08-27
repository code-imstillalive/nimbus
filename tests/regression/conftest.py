"""Fixtures for data-driven regression tests over captured diagnostic JSONs.

These tests are deliberately hermetic against the running integration: they
load a real captured `diagnostics.json` (Settings → Devices & Services →
Nimbus → Download diagnostics) plus, optionally, real captured source-sensor
state JSONs from `/api/states/<entity_id>`, and assert invariants over the
resulting `sensor.nimbus_solver_battery_forecast` `forecast[]` list.

The intent (see repo issue #217) is to keep a small set of representative
"golden" installs' captures alongside the code so that every merge is
required to preserve the invariants they exercised — turning ad-hoc IV&V
reports into standing regression tests.

Fixtures under `tests/regression/fixtures/<install_name>/` follow this
convention::

    fixtures/purcell_qld1/
      README.md                           # provenance, install shape, capture time
      nimbus_diag.json                    # full diag JSON from /api/diagnostics/config_entry/<id>
      nimbus_solver_battery_forecast.json # /api/states/sensor.nimbus_solver_battery_forecast
      amber_ex_feed_in.json               # /api/states/sensor.amber_express_amber_feed_in_price (optional)
      amber_ex_general.json               # /api/states/sensor.amber_express_amber_general_price (optional)

Each install directory is one parametrised test case; per-file presence
gates the source-sensor comparisons (e.g. PRICE-01 only runs where
`amber_ex_feed_in.json` is available).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _list_installs() -> list[str]:
    if not FIXTURES.exists():
        return []
    return sorted(
        p.name
        for p in FIXTURES.iterdir()
        if p.is_dir() and (p / "nimbus_diag.json").exists()
    )


@pytest.fixture(params=_list_installs())
def install_dir(request) -> Path:
    """One captured install directory, parametrised across all fixtures."""
    return FIXTURES / request.param


@pytest.fixture
def nimbus_diag(install_dir: Path) -> dict:
    """Full diagnostic JSON — the payload the Download diagnostics button emits."""
    return json.loads((install_dir / "nimbus_diag.json").read_text())


@pytest.fixture
def nsbf_state(install_dir: Path) -> dict:
    """State of sensor.nimbus_solver_battery_forecast at capture time.

    Falls back to the diagnostic's own embedded solver-forecast section when the
    dedicated per-entity capture is absent.
    """
    dedicated = install_dir / "nimbus_solver_battery_forecast.json"
    if dedicated.exists():
        return json.loads(dedicated.read_text())
    # fall back to whatever the diag surfaces
    return json.loads((install_dir / "nimbus_diag.json").read_text())


@pytest.fixture
def forecast(nsbf_state: dict) -> list[dict]:
    """The forecast[] list itself — what the LP wrote out for the plan horizon.

    Accepts either the dedicated /api/states/... shape (top-level `attributes`)
    or falling back to searching the diagnostic dump for the same list.
    """
    attrs = nsbf_state.get("attributes")
    if attrs and "forecast" in attrs:
        return attrs["forecast"]
    # last-resort search through the diagnostic
    stack = [nsbf_state]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if (
                "forecast" in cur
                and isinstance(cur["forecast"], list)
                and cur["forecast"]
            ):
                first = cur["forecast"][0]
                if (
                    isinstance(first, dict)
                    and "battery_kw" in first
                    and "soc_pct" in first
                ):
                    return cur["forecast"]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    pytest.skip("no forecast[] found in this fixture")


@pytest.fixture
def solver_config(nimbus_diag: dict) -> dict:
    """The resolved `solver_config` — the flat dict Nimbus itself uses at
    solve time (`solver_battery_capacity_kwh`, `solver_max_charge_kw`,
    `solver_max_discharge_kw`, `solver_battery_min_soc_percent`, ...).

    Handles the current /api/diagnostics/config_entry/<id> shape
    (`data.solver_config`) and falls back to older `entry.options` /
    `data.entry.options` layouts.
    """
    data = nimbus_diag.get("data")
    if isinstance(data, dict):
        sc = data.get("solver_config")
        if isinstance(sc, dict) and sc:
            return sc
        entry = data.get("entry")
        if isinstance(entry, dict) and isinstance(entry.get("options"), dict):
            return entry["options"]
    if isinstance(nimbus_diag.get("entry"), dict):
        opts = nimbus_diag["entry"].get("options")
        if opts:
            return opts
    if isinstance(nimbus_diag.get("options"), dict):
        return nimbus_diag["options"]
    pytest.skip("no solver_config resolvable in this fixture")


def _try_load(install_dir: Path, name: str) -> dict | None:
    p = install_dir / name
    if not p.exists():
        return None
    return json.loads(p.read_text())


@pytest.fixture
def fixture_skips(install_dir: Path) -> set[str]:
    """Set of invariant prefixes (e.g. ``SET``) that this fixture explicitly
    opts out of, one prefix per non-blank line in ``SKIP_INVARIANTS.txt``.

    Use this only when a fixture is deliberately kept as a historical record
    of a pre-fix state (e.g. a golden captured before the #220 blend-bypass
    landed). Prefer capturing a fresh fixture over adding skips.
    """
    p = install_dir / "SKIP_INVARIANTS.txt"
    if not p.exists():
        return set()
    return {
        line.strip().split("#", 1)[0].strip()
        for line in p.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


@pytest.fixture
def amber_ex_feed_in(install_dir: Path) -> dict:
    """/api/states/sensor.amber_express_amber_feed_in_price — export-side source
    truth for the PRICE-01 regression. Skipped if absent (test only runs against
    installs that actually use Amber Express)."""
    d = _try_load(install_dir, "amber_ex_feed_in.json")
    if d is None:
        pytest.skip("amber_ex_feed_in.json not captured for this install")
    return d


@pytest.fixture
def amber_ex_general(install_dir: Path) -> dict:
    """/api/states/sensor.amber_express_amber_general_price — import-side source
    truth for the PRICE-02 regression."""
    d = _try_load(install_dir, "amber_ex_general.json")
    if d is None:
        pytest.skip("amber_ex_general.json not captured for this install")
    return d
