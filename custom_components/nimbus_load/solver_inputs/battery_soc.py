"""The battery's SoC envelope for one Solver cycle: configured percents
resolved to kWh, the live reading validated against them, and the
round-trip efficiency the LP charges.

nimbus issue #735, stage 5. Measured before moving, same as every stage
before it — and the measurement is the reason this block is here and its
neighbour is not.

The issue proposes a `solver_plan` module covering the LP setup. An AST
walk of that whole region (lines resolving the SoC bounds through the
`elements.*Config` construction) reports **26 inputs and 8 outputs** —
which would be a 26-parameter function, strictly worse to read than the
inline code it replaced. Split at the seam the measurement actually
shows, it is two very different halves:

| half | lines | inputs | outputs |
|---|---|---|---|
| **this one** — SoC envelope, validation, efficiency | 91 | 5 | 4 |
| element construction (`BatteryConfig`/`GridConfig`/…) | 82 | **27** | 6 |

Five in and four out is narrower than either seam already extracted
(solar: three out; load: four in, twelve out). The other half is a
*constructor call*, not logic — extracting it would move an argument
list into a second file and add a hop for nothing. **It is deliberately
left in `main()`**, and that is a finding about this issue's own proposed
module list, not an omission.

Two behaviours in here are load-bearing and were, until this extraction,
guarded by nothing at all — no test in the suite referenced either:

- **nimbus issue #328: the starting SoC is passed through honestly, never
  clamped to the configured floor/ceiling.** The 2026-08-23 clamp this
  replaced stopped a real crash loop, but by reporting a *fictional*
  in-range SoC to the LP — every downstream number (planned throughput,
  `total_cost`, the next cycle's own starting assumption, the quality
  report's EPR) was then quietly wrong by the clamped gap. A "tidy-up"
  that reinstates a clamp here re-breaks all of it silently.
- **nimbus issue #601: the excursion warning fires once, then DEBUG, then
  logs a recovery.** The 8 Sep day spent at 0% would otherwise have
  logged it ~800 times overnight.

A **physical** clamp to `[0, capacity_kwh]` does remain, and is a
different thing from the one #328 removed: it exists because a single
glitch sensor reading (negative, or over 100%) would otherwise fail
`elements.BatteryConfig`'s own invariant and kill the periodic solve —
the original 27-crashes-per-window incident. The two clamps are easy to
confuse and the tests beside this module pin both.

`_HOME_BATTERY_SOC_EXCURSION_WARNED` moves here with the code that uses
it. Every reference to it in the repo was inside this block, so the flag
had no business living three thousand lines away from its only reader.

See `solver_inputs/__init__.py` for why the `solver_writer` import is
deferred and by-module; the reasoning is identical and load-bearing.
"""

from __future__ import annotations

from dataclasses import dataclass

# nimbus issue #601: warn-once/debug/recovered state for the excursion
# log below — a plain module-level flag rather than a keyed set, since
# there is only ever one "home" battery (unlike battery participants,
# which `solver_writer._BATTERY_PARTICIPANT_WARNED` keys by name).
_HOME_BATTERY_SOC_EXCURSION_WARNED = False


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) — see this package's `__init__.py` for the full reasoning, and
    #861 for the concrete failure that made it load-bearing."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


@dataclass(frozen=True)
class SocEnvelope:
    """The four values this slice produces.

    `min_soc_kwh`/`max_soc_kwh` are the *soft* preference the LP costs
    recovery toward, not hard bounds — `initial_soc_kwh` is allowed to
    sit outside them, and on a real excursion it does. Carrying all four
    together is what stops a caller reconstructing one of them from the
    percents and getting a subtly different number.
    """

    initial_soc_kwh: float
    min_soc_kwh: float
    max_soc_kwh: float
    charge_discharge_efficiency: float


def resolve_soc_envelope(
    cfg,
    *,
    capacity_kwh: float,
    min_pct: float,
    max_pct: float,
    initial_pct: float,
) -> SocEnvelope:
    """Resolve the configured percents to kWh and validate the live SoC.

    The three percents are keyword-only deliberately: they are three
    bare floats of the same unit, and a transposed `min`/`initial` pair
    would produce a plausible envelope and a wrong plan with nothing to
    catch it.
    """
    global _HOME_BATTERY_SOC_EXCURSION_WARNED

    sw = _solver_writer()

    max_soc_kwh_val = capacity_kwh * max_pct / 100.0
    min_soc_kwh_val = sw.resolve_min_soc_kwh(min_pct, capacity_kwh, max_soc_kwh_val)
    initial_soc_kwh_raw = capacity_kwh * initial_pct / 100.0

    # nimbus issue #328 (Mark Purcell) -- honest pass-through, no clamp.
    # The 2026-08-23 clamp this replaced stopped the real 27+-crashes-per-
    # window incident (a live SoC sensor reading below/above the
    # configured floor/ceiling used to crash elements.BatteryConfig's own
    # invariant, propagating a ValueError up through async_track_time_
    # interval every minute), but it did so by silently reporting a
    # FICTIONAL in-range starting SoC to the LP -- every downstream
    # number (planned throughput, total_cost, next cycle's own starting
    # assumption, and the quality-report scorer's EPR ratio) was then
    # quietly wrong by the clamped gap, with only a single WARN log
    # naming that it happened. elements.BatteryConfig.__post_init__ now
    # only requires initial_soc_kwh to sit inside the PHYSICAL range [0,
    # capacity_kwh] (never raises for a below-floor/above-ceiling value
    # on its own), and build_plan()'s own soc[t]/underfill[t]/overfill[t]
    # construction treats min_soc/max_soc as a SOFT, costed preference
    # the LP schedules real recovery toward -- so the raw, true value can
    # go straight to the LP honestly, with both sides of any downstream
    # comparison (this solve and the quality-report scorer) seeing the
    # same real state.
    initial_soc_kwh = initial_soc_kwh_raw
    # nimbus issue #601: same warn-once/debug/recovered treatment as
    # build_extra_batteries()'s own per-participant SoC-excursion warning
    # -- the 8 Sep day at 0% would otherwise have logged this ~800 times
    # overnight. See _HOME_BATTERY_SOC_EXCURSION_WARNED's own module-
    # level comment above for why this is a plain flag, not a set entry.
    if not (min_soc_kwh_val <= initial_soc_kwh_raw <= max_soc_kwh_val):
        _initial_pct_raw = (
            initial_soc_kwh_raw / capacity_kwh * 100.0 if capacity_kwh > 0 else 0.0
        )
        if not _HOME_BATTERY_SOC_EXCURSION_WARNED:
            _HOME_BATTERY_SOC_EXCURSION_WARNED = True
            sw._LOGGER.warning(
                "Nimbus Solver: live battery SoC %.2f%% is outside the "
                "configured Solver floor/ceiling [%.2f%%, %.2f%%] -- the LP "
                "is scheduling real recovery this cycle rather than having "
                "this state clamped away. If this repeats every period the "
                "real battery is stuck outside its own configured range "
                "(fault, cold pack, sensor drift) -- investigate rather "
                "than lower the floor. (Logged once per excursion; further "
                "cycles are DEBUG until it recovers.)",
                _initial_pct_raw,
                min_pct,
                max_pct,
            )
        else:
            sw._LOGGER.debug(
                "Nimbus Solver: live battery SoC %.2f%% still outside the "
                "configured Solver floor/ceiling [%.2f%%, %.2f%%] this "
                "cycle.",
                _initial_pct_raw,
                min_pct,
                max_pct,
            )
    elif _HOME_BATTERY_SOC_EXCURSION_WARNED:
        _HOME_BATTERY_SOC_EXCURSION_WARNED = False
        _initial_pct_raw = (
            initial_soc_kwh_raw / capacity_kwh * 100.0 if capacity_kwh > 0 else 0.0
        )
        sw._LOGGER.info(
            "Nimbus Solver: live battery SoC %.2f%% has recovered back "
            "inside the configured Solver floor/ceiling [%.2f%%, %.2f%%].",
            _initial_pct_raw,
            min_pct,
            max_pct,
        )
    # A genuinely PHYSICAL clamp still has to stay, same reasoning as
    # _compute_report_for_window()'s own site (see that comment) --
    # elements.BatteryConfig still rejects a value outside the true
    # physical range [0, capacity_kwh], and this loop runs every solve
    # cycle against a live sensor, so a single glitch reading (>100% or
    # negative) must not crash the periodic solve the way the original
    # 27+-crashes-per-window incident did.
    if not (0.0 <= initial_soc_kwh_raw <= capacity_kwh):
        initial_soc_kwh = min(max(initial_soc_kwh_raw, 0.0), capacity_kwh)
        sw._LOGGER.warning(
            "Nimbus Solver: live battery SoC reading is outside the "
            "battery's own PHYSICAL range [0, %.2f kWh] -- clamping to "
            "%.4f kWh to keep this solve alive. This is sensor nonsense "
            "(calibration drift, a template-averaging overshoot), not a "
            "real state -- investigate the sensor if this recurs.",
            capacity_kwh,
            initial_soc_kwh,
        )
    charge_discharge_efficiency = (
        min(sw._cfg_num(cfg, "solver_efficiency_percent", 95.0) / 100.0, 0.999) ** 0.5
    )
    return SocEnvelope(
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=min_soc_kwh_val,
        max_soc_kwh=max_soc_kwh_val,
        charge_discharge_efficiency=charge_discharge_efficiency,
    )
