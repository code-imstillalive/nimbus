"""#2392 entity-disabled-by-default: a diagnostic entity starts disabled on a
new install, unless Nimbus's own tabs or cards display it, so no tab row or
card tile ever reads an entity a new install has switched off.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.nimbus_load import sensor, sensor_flattened

_PKG = Path(sensor_flattened.__file__).resolve().parent
_DISPLAY_SOURCES = [
    _PKG / "solver_tab.py",
    _PKG / "help_tab.py",
    _PKG / "forecaster_dashboard.py",
    *sorted((_PKG / "frontend").glob("*.js")),
]


def _all_specs() -> list[sensor_flattened.FlattenedAttrSpec]:
    return [
        spec
        for value in vars(sensor_flattened).values()
        if isinstance(value, tuple)
        for spec in value
        if isinstance(spec, sensor_flattened.FlattenedAttrSpec)
    ]


def test_no_tab_or_card_reads_an_entity_a_new_install_disables():
    text = "\n".join(p.read_text(encoding="utf-8") for p in _DISPLAY_SOURCES)
    diagnostic = [
        s
        for s in _all_specs()
        if s.entity_category == sensor_flattened.EntityCategory.DIAGNOSTIC
    ]
    assert diagnostic, "sanity: the walk found the diagnostic specs"

    # Flattened sensors: a tab or card names them as nimbus_<prefix>_<suffix>.
    shown_but_disabled = sorted(
        s.entity_id_suffix
        for s in diagnostic
        if re.search(
            r"nimbus_[a-z0-9_]*?_" + re.escape(s.entity_id_suffix) + r"\b", text
        )
        and not sensor_flattened.enabled_by_default(s)
    )
    assert shown_but_disabled == [], (
        "displayed by a tab or card but disabled by default on a new install: "
        f"{shown_but_disabled}; add them to SHOWN_ON_DASHBOARD"
    )

    # The kept set holds only real diagnostic suffixes, so it can't go stale.
    stale = sorted(
        sensor_flattened.SHOWN_ON_DASHBOARD - {s.entity_id_suffix for s in diagnostic}
    )
    assert stale == [], f"SHOWN_ON_DASHBOARD names no diagnostic sensor: {stale}"

    # Controllable-load sensors reach the Topology card through the config
    # sensor.py builds for each load (`entity(sid, "<suffix>")`), not by name
    # in the card's source.
    passed = set(
        re.findall(
            r'entity\(sid, "([a-z_]+)"\)',
            Path(sensor.__file__).read_text(encoding="utf-8"),
        )
    )
    schedule = {
        cls._ENTITY_ID_SUFFIX: cls
        for cls in vars(sensor).values()
        if isinstance(cls, type)
        and issubclass(cls, sensor._NimbusControllableLoadScheduleSensorBase)
        and cls._ENTITY_ID_SUFFIX
    }
    assert schedule, "sanity: the walk found the controllable-load sensors"
    passed_but_disabled = sorted(
        suffix
        for suffix in passed & set(schedule)
        if not schedule[suffix]._attr_entity_registry_enabled_default
    )
    assert passed_but_disabled == [], (
        f"passed to the Topology card but disabled by default: {passed_but_disabled}"
    )
