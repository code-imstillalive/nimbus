# API contract

What's stable, what isn't, and what a version bump means when it changes.
Written in direct response to nimbus repo issue #217 (Mark Purcell, "14-day
project health assessment"): a downstream automation or regression fixture
(`tests/regression/`) that keys on an entity attribute needs to know which
attributes it can build on without re-checking every release, and which are
still finding their shape.

This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for the **stable subset** defined below, starting from this document's own
introduction (v0.94.6). Versions before this one made no such distinction —
don't assume anything about pre-0.94.6 behaviour from this doc.

**A version bump's meaning, going forward:**
- **PATCH** (`0.94.x`): bug fixes, no stable-subset shape change.
- **MINOR** (`0.x.0`): a new stable attribute/entity added, or a new
  currently-unstable field promoted to stable. Existing stable fields
  unchanged.
- **MAJOR**: a stable field's name, type, unit, or sign convention changes,
  or a stable field is removed. Announced in `CHANGELOG.md` with a migration
  note before it ships, not just discovered by diffing a diagnostic.

This is a real constraint on the codebase, not just a promise: it means a
fix to a bug like #216/#220 (a stable field was computing the WRONG value)
is a PATCH — the field's contract didn't change, its output finally matches
that contract. A fix that would change what a stable field *means* (unit,
sign, or shape) is a MAJOR, even if it's "just" a bug fix from the author's
perspective.

## Why v1.0.0 isn't declared yet

Nimbus stays in shadow mode (see the top-level `README.md`'s "Status and
roadmap") until the reference-household readiness checklist is green,
independent of this document. Issue #211 (sustained over-frequent writes on
the two aggregate push-sensors, root cause still open as of this writing) is
exactly the kind of live, unresolved correctness issue that should block a
"1.0, this is graduated" declaration — a version number is a promise, and
`v1.0.0` promising stability while a known write-frequency bug is open would
be a promise this project can't currently keep. The stable subset below is
usable and SemVer-protected starting now; the *ceremony* of calling it 1.0
waits for #211 (and the rest of the checklist) to close first.

## Stable subset

### `sensor.nimbus_solver_battery_forecast`

Top-level attributes:

| Attribute | Type | Notes |
|---|---|---|
| `status` | string | `"optimal"` on a normal solve. Any other value means the LP didn't reach a clean solution this cycle — don't trust `forecast` values when this isn't `"optimal"`. |
| `generated_at` | ISO 8601 datetime | Wall-clock time this specific solve was produced. A staleness check (`now() - generated_at`) is the correct way to detect a stuck coordinator — see issue #211's own history for why. |
| `total_cost` | float, $ | Solved objective value over the full horizon. |
| `total_cost_with_fixed_costs` | float, $ | `total_cost` plus any configured flat/fixed charges. |
| `equivalent_full_cycles` | float | Battery throughput this horizon, expressed as full-capacity-equivalent cycles. |
| `total_throughput_kwh` | float | Raw kWh moved through the battery (charge + discharge), gross. |
| `total_charge_kwh` / `total_discharge_kwh` | float | Gross one-directional totals — use these, not a net sum of `forecast[].battery_kw`, to reconcile energy balance (see issue #217's LP-03 finding for why the distinction matters). |
| `battery_kw_side` | string | Always `"AC"` today — `battery_kw` is grid-side of the inverter, not DC/battery-side. |
| `battery_kw_sign_convention` | string | Always `"positive_discharge_negative_charge"` today. |
| `efficiency_convention` | string | Always `"round_trip_symmetric_sqrt"` today — `solver_efficiency_percent` is a single round-trip figure, applied as `sqrt(round_trip)` to each direction independently. |
| `charge_efficiency` / `discharge_efficiency` | float, 0-1 | The actual per-direction efficiency values the LP solved with this cycle. |
| `ac_bus_losses_kwh` | float | Implied $ value of round-trip losses over this horizon's `total_charge_kwh`/`total_discharge_kwh`. |
| `binding_constraint_now` | string | Human-readable label for whatever constraint is actively limiting period-0 dispatch. |
| `n_clamped_periods` | int | How many periods this solve had to clamp a variable against a hard bound. |

`forecast[]` (list, one entry per planning period), stable fields per entry:

| Field | Type | Notes |
|---|---|---|
| `time` | ISO 8601 datetime | Period start. |
| `hours` | float | Real period duration — NOT a fixed 0.25 assumption; the horizon is tiered (fine near-term, coarse further out). |
| `battery_kw` | float | See `battery_kw_side`/`battery_kw_sign_convention` above. |
| `battery_kw_after_efficiency` | float | Physical, post-efficiency energy rate at the battery terminals (issue #229) — `battery_kw` is the LP's own pre-efficiency decision variable, so `(soc_pct[0] - soc_pct[-1]) / 100 * capacity_kwh` only reconciles against `Σ(battery_kw_after_efficiency × hours)`, not against `battery_kw` directly. |
| `soc_pct` | float, 0-100 | |
| `grid_import_kw` / `grid_export_kw` | float, ≥0 | |
| `solar_kw` / `load_kw` | float, ≥0 | |
| `import_price` / `export_price` | float, $/kWh | The full landed price the LP actually optimised against (fees, blending, and the settled-current-block override from issue #220 all already applied). |
| `import_price_raw` / `export_price_raw` | float, $/kWh | The configured source sensor's own value BEFORE any secondary-source blend (issue #216/#220) — a genuine "what did the source actually say" probe, not the LP's landed price. Byte-identical to the `_price` field on any single-source install with no `_sensor_2`/`_sensor_3` configured. |
| `net_cost` | float, $ | This period's contribution to `total_cost`. |

### `sensor.nimbus_solver_config`

`state` is `"configured"` once every required Solver field has a value, and
stays `"unconfigured"` (with `unresolved_required_keys` naming what's
missing) otherwise — this is the correct machine-readable health check, not
a keys-present heuristic on the config entry. Every attribute this entity
exposes is a live mirror of the corresponding `number.nimbus_solver_*`
entity or config-flow field and is stable by construction (it's already
covered by those entities' own stability below).

### `number.nimbus_solver_*` entities

Every entity in this family (battery capacity/SoC bounds/charge-discharge
limits/efficiency, grid import-export limits, network fee blocks, flat fee
rate, economics, risk aversion, P2P blocks) is stable: its `entity_id`,
unit, and meaning don't change without a MAJOR bump. New `number.nimbus_solver_*`
entities may be added in a MINOR release (e.g. a new fee block); existing
ones are never silently repurposed.

### Config-flow keys

Every key documented in `docs/configuration-reference.md` is stable —that
page is generated from the real source specifically so it can't drift, and
this contract extends to it directly rather than duplicating the field list
here.

## Not yet stable

Anything not named above, including but not limited to:

- Per-load forecast sensors' own attribute shape beyond the four fields
  RAW-01/RAW-02 already cover (`import_price_raw`, `export_price_raw`,
  `load_kw`, `solar_kw`) — the rest of a per-load `forecast[]` entry's shape
  is still settling.
- `sensor.nimbus_topology_config` and the switchboard-topology-card's own
  data contract — actively changing as topology rendering matures.
- Diagnostic-only fields (`solar_delivery_ratio`, `p2p_recent_avg_volume_kwh`,
  `price_blend_algorithm`, and similar) — genuinely useful today, not yet
  promoted because they haven't had a full release cycle of real-world use
  to confirm their final shape.
- Everything under `nimbus_solver_app` (the deprecated standalone add-on,
  removed in v1.0.0 per the top-level README) — no new stability commitment
  is being made to a path already scheduled for removal.

A field moving from this list into the stable subset above is a MINOR bump,
announced in `CHANGELOG.md`.

## How to propose a promotion

Open an issue naming the field, how long it's been stable in practice (a
release count, not a calendar duration — a field re-verified every release
across several is a much stronger case than the same wall-clock time with
no re-checks), and ideally a `tests/regression/` invariant that would catch
a future regression on it. This is exactly the same evidence bar issue #216
and #220 met organically — codifying it here just makes the ask explicit
rather than something a tester has to infer.
