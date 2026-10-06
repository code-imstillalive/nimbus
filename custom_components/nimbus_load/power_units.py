"""nimbus issue #1570: the one power-unit converter.

Every power reading Nimbus takes -- the Forecaster's training and live
features, the Solver's live and historical inputs, its reports -- goes
through `power_to_kw()` / `power_scale_to_kw()`. Before this, about seven
places each carried their own copy of `0.001 if unit == "W" else 1.0`
(exact and case-sensitive, so "w", "MW" and "GW" were read as kW), one live
read had no conversion at all, and the Forecaster used Home Assistant's
PowerConverter while the Solver used its own checks, so the two could
disagree about the same sensor.

The cards (frontend/*.js) carry the same table as `NIMBUS_POWER_TO_KW`;
`tests/test_1570_power_units.py` checks each card's table against this one.

HA-import-free on purpose: the Solver also runs standalone, without Home
Assistant. The units match HA's `UnitOfPower`.
"""

from __future__ import annotations

# kW per one unit. Keys are casefolded; lookups strip and casefold, so "W",
# "w" and " kW " all resolve.
POWER_TO_KW: dict[str, float] = {
    "mw": 1e3,  # megawatt (casefold of "MW"); milliwatt below is "mW"
    "w": 1e-3,
    "kw": 1.0,
    "gw": 1e6,
    "tw": 1e9,
    "btu/h": 0.00029307107,
}
# "mW" (milliwatt) and "MW" (megawatt) collide once casefolded. A milliwatt
# house sensor does not occur in practice, a megawatt one does (aggregated
# meters), so the casefolded "mw" means megawatt; an exact "mW" is checked
# first and means milliwatt.
_EXACT: dict[str, float] = {"mW": 1e-6, "MW": 1e3}


def is_power_unit(unit: object) -> bool:
    """True when `unit` is a power unit this converter knows."""
    if not isinstance(unit, str):
        return False
    u = unit.strip()
    return u in _EXACT or u.casefold() in POWER_TO_KW


def power_scale_to_kw(unit: object) -> float:
    """Multiplier that brings a reading in `unit` to kW.

    No unit, or one that is not a power unit, gives 1.0: kW was always the
    assumption, and an energy or other non-power unit is caught separately
    (power_input_check.py, nimbus #1562) rather than guessed at here.
    """
    if not isinstance(unit, str):
        return 1.0
    u = unit.strip()
    if u in _EXACT:
        return _EXACT[u]
    return POWER_TO_KW.get(u.casefold(), 1.0)


def power_to_kw(value: float, unit: object) -> float:
    """`value` in `unit`, expressed in kW."""
    return value * power_scale_to_kw(unit)
