# CLAUDE.md — Nimbus

Instructions for any Claude instance working on this repo. Read this before touching any file.

---

## ⚠️ PRIME DIRECTIVE — ZERO HAEO

> **NEVER REFERENCE HAEO IN NIMBUS. NOT A SENSOR. NOT AN ENTITY. NOT A FEATURE. NEVER.**
>
> Nimbus is being built to eventually become the household's own replacement for HAEO —
> not a companion tool that reads HAEO's sensors, not a fallback, not a comparison. A full
> independent replacement.
>
> **Why:** HAEO has been a genuine, repeated source of instability for this household
> (Infeasible/unavailable crashes, LP wash-trade degeneracy, dead forecast-source
> references, intermittent failures needing a second restart to clear — see the sibling
> `116KAT-HA-AI` repo's own CLAUDE.md for the extensive documented history). The whole
> point of Nimbus is to not be dependent on any of that.
>
> **Rules:**
> - Never wire in an entity that is HAEO's own plan/forecast output — identifiable by
>   carrying a `forecast` attribute that mirrors HAEO's optimizer plan (e.g.
>   `sensor.battery_active_power`, `sensor.grid_active_power`, `sensor.solar_power`,
>   `number.grid_export_price`, `number.grid_import_price`, `sensor.battery_discharge_power`,
>   `sensor.battery_charge_power`) — not as a training feature, not as a display source,
>   not "just for comparison."
> - If Nimbus (or a Nimbus-adjacent dashboard) needs visibility into Battery/Solar/Grid,
>   use REAL MEASURED entities instead — genuinely independent of whether HAEO is
>   installed, running, or healthy at all.
> - If a genuine forward-looking forecast of Battery/Solar/Grid is wanted, the only
>   honest paths are: (a) a real non-HAEO forecaster for that specific signal (e.g.
>   Solcast/Open-Meteo for solar), or (b) Nimbus's own ML pipeline learning to forecast
>   it from real recorder history, the same way it already forecasts loads — never
>   borrowing HAEO's own LP-derived plan.
> - This was violated once already (2026-08-15, a "Power Balance" dashboard chart built
>   against `sensor.grid_active_power`/`battery_active_power`/`solar_power` — all three
>   HAEO plan sensors) — caught, PR closed unmerged. Don't repeat it.

---

## What Nimbus is

A Home Assistant custom_component (`custom_components/nimbus_load`) that today forecasts
individual load power draw (HWS, pool, EV charger, AC zones, etc.) from real recorder
history — pure numpy, no scikit-learn (no C compiler / no wheel available inside HA's
own container). Two model types (k-NN, GBRT), validated against each other and a
seasonal-naive baseline on every retrain, with genuine model-derived confidence bands
where available. See `custom_components/nimbus_load/ml/model.py`'s own module docstring
for the full technical detail — it's kept current there, not duplicated here.

That's stage 1 of a longer destination, not the finished product.

## Roadmap — Forecasters → Topology → Solver

Stated goal (2026-08-15, verbatim): *"we are building NIMBUS to be a solver optimised
down the track... smarter than emhass haeo etc etc... we are combing through FORECASTERS
FIRST - building better forecaster, then TOPOLOGY MAP better system to see loads, then
SOLVER to manage and optimise batteries solar and loads."* Three deliberate stages, in
this order, each one a real prerequisite for the next:

**1. Forecasters (current stage).** Get individual-signal prediction genuinely right
before trying to optimize anything. This started as load-only (HWS/pool/EV/AC/etc.), then
extended (2026-08-15) to give the load models real system-context visibility — battery/
grid/solar power as additional input features — because a load's real behaviour is
confounded by what else is happening on the switchboard at the same moment (a load looks
different at "10am, 22C" depending on whether the battery happens to be mid-charge right
then). The natural next extension within this stage: Nimbus's own ML pipeline learning to
forecast Battery/Solar/Grid themselves as genuine targets (not just load-model inputs),
the same k-NN/GBRT/validation machinery already proven for loads — real measured history
in, real validated forecast out, no HAEO involved at any point.

**2. Topology map.** A real visual representation of how the household's power system is
actually wired — switchboard, inverters, battery towers, PV strings, loads, and how they
connect — so both a human and (eventually) the solver can see the real system shape, not
just a flat list of sensors. `custom_components/nimbus_load` itself won't contain this
(it's a dashboard/visualization concern, not a forecasting one) — see the sibling
`116KAT-HA-AI` repo's own topology-card work for the current state of this piece.

**3. Solver.** The actual optimization/decision engine — deciding how to manage battery
charge/discharge, solar (curtail or not), and load scheduling, i.e. HAEO's and EMHASS's
own actual job today, done independently and (the whole point) more reliably. Will need
its own real configuration surface eventually — efficiencies, cost policies, salvage
value, the same category of settings HAEO exposes today, but Nimbus's own, not borrowed.
Not started, not scoped in detail yet — noted here so the eventual shape of `const.py`/
the config flows isn't a surprise when this stage begins.

**Why this order, not solver-first:** a solver making decisions on top of forecasts it
can't trust is worse than no solver at all — this is explicitly why forecasting accuracy
(seasonal-naive baseline, MASE, real validated model selection, genuine confidence bands)
got this much rigor before any optimization logic was even discussed.

## Deploy

Deployed on the household's own NUC (see the sibling `116KAT-HA-AI` repo) as a direct
git clone at `/opt/homeassistant/config/nimbus_repo`, symlinked into
`custom_components/nimbus_load`. Deploy:
```
cd /opt/homeassistant/config/nimbus_repo && git fetch origin && git pull origin main
docker restart opt_homeassistant_1
```
A Python custom_component change always needs a full restart — a config reload cannot
reload changed Python modules, only YAML/config.

## Git workflow

Branch + PR for every real change, same as `116KAT-HA-AI`. No direct pushes to `main`.
This was violated a few times early on (direct-to-main pushes) before being fixed —
don't repeat that either.

## Testing

No scikit-learn/pytest infra inside the HA container this trains in, so verification
happens locally: copy the relevant `custom_components/nimbus_load/{const.py,ml/*.py}`
files (pure numpy + stdlib, zero HA dependencies) into a scratch test package and run
real synthetic data through `train_model()`/`predict()` before shipping — not just a
syntax check. See recent PR descriptions for the pattern.
