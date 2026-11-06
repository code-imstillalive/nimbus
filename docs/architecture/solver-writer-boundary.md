# solver_writer.py: where the standalone mode ends (design note, #2289 item 5)

Status: **proposal for review** (Mark Purcell, household). Nothing moves until this
note is agreed. Facts measured on `main` at `bfbf6fc1` (0.96.0), 6 Nov 2026.

## What is there

`custom_components/nimbus_load/solver_writer.py` is 10,223 lines and runs in two modes:

| | in-process (production) | standalone REST |
|---|---|---|
| started by | `solver_runtime.py`: a 1-minute timer, the solve in `hass.async_add_executor_job` | `python3 solver_writer.py` (`if __name__ == "__main__"` → `main()`) |
| reads/writes HA through | `hass.states`, the recorder, services (after `set_native_hass(hass)`) | `urllib` against `HA_BASE` with a token file (`HA_TOKEN_PATH`, default a household path) |
| who uses it | every HACS install | nobody known (see the open question below) |

The original design (Aug 2026) was a **three-function seam**: `ha_get`, `ha_post_state`
and `fetch_price_history` branch on whether a `hass` was injected, and everything else
is mode-blind. That seam has leaked:

- **26 top-level functions in `solver_writer.py`** read `NATIVE.hass`, `urllib`, `HA_BASE`
  or the token directly. For example: `fetch_solver_config`, `ha_call_service`,
  `fetch_entity_power_history_kw`, `fetch_aemo_forecast`,
  `repair_provisional_quality_history`, `_discover_power_sensor_for_device`.
- **Branch sites elsewhere:**
  - 10 in `solver_shared.py`;
  - 8 in `solver_inputs/controllable_loads.py`, 5 in `solver_dispatch/guard.py`, and
    1–2 each in four other `solver_inputs` modules;
  - 1 each in `solver/cycle_lock.py` and `solver_runtime.py`.
- `urllib.request` is called directly in `solver_writer.py` (8 sites), `solver_shared.py`
  (10) and `solver_reports/quality.py`.
- Module import still evaluates the standalone settings (`HA_BASE`, `TOKEN_PATH`, the
  Supervisor branch). #2289 item 8 removed the private-repo pointers; the household
  default path for the token remains.

## How each mode is tested

The golden master (`tests/golden/`, the regression net every refactor is accepted
against) runs `solver_writer.main()` in a child process. **16 of its 22 scenarios run in
standalone REST mode** against a fake HTTP server (`fake_ha.FakeHA`). Only 6 inject a
fake `hass` (`fake_native`, #1335), which is the mode every real install uses.

So today the REST mode is the better-tested of the two, and production's mode is the
less-tested. Any split has to keep the golden master green and byte-identical. That is
the constraint that decides the order below.

## Open question (blocks the choice)

**Does anyone run `solver_writer.py` standalone?** The Supervisor add-on that used it was
removed in v0.94.85 (#357). The reference household's scheduled scripts are separate
files that import `solver/` directly, not `solver_writer.py`. This needs confirming on
their machines, which only the household can do.

## Option A: keep both modes, behind one port

1. Define one interface, `HaPort`: `get_state`, `post_state`, `call_service`,
   `call_service_with_response`, `history(entity, start, end)`,
   `statistics(entity, start, end)`. Two implementations:
   - `NativePort(hass)`;
   - `RestPort(base, token)`, moved to `solver_rest.py`.
2. Route the 26 functions and the branch sites through `HaPort`, one module per PR, with
   the golden master byte-identical after each. No behaviour change.
3. The `__main__` entry and the `HA_BASE`/token/Supervisor settings move to a small
   `scripts/solver_standalone.py` that builds a `RestPort` and calls the engine.
4. Then `solver_writer.py` can be split by responsibility (inputs, solve, publish,
   reports): each part is already behind the port, so a split moves code, not
   behaviour.

Cost: the most PRs. Both modes stay supported, and the REST mode stays tested only
through the golden master.

## Option B: retire the standalone mode

1. Give each of the 16 REST golden scenarios a native twin, one batch per PR, keeping
   the REST original. A snapshot cannot stay byte-identical across modes: native reads
   are recorded as `NATIVE_GET`, and native-only branches run. What must match is what
   the twin posts. The first batch (#2324, the three synthetic scenarios) posts exactly
   the same entities and states as its REST originals. `fake_native` already served 6.
   This raises the coverage of production's own mode before anything is removed.
2. Delete the REST originals with the REST branches, `urllib` calls, `HA_BASE`/`TOKEN_PATH`/Supervisor settings
   and `__main__`, one module per PR, with the golden master byte-identical.
3. Then split `solver_writer.py` by responsibility, as in A.4.

Cost: fewer PRs, and the code ends up smaller and single-mode. A user of the standalone
mode, if there is one, loses it, so B needs the open question answered "nobody", and a
CHANGELOG line.

## Recommendation

**B**, if the household confirms no machine runs `solver_writer.py` standalone.
Production's mode becomes the tested one first, which is worth doing whichever option
is chosen. Otherwise **A**.

Either way, step 1 changes no behaviour and can start once this note is agreed: B.1
moves golden scenarios to native, and A.1 adds `HaPort` with `NativePort` only.

## Not in scope

- `solver/` (the HA-free model) is untouched; the import-linter contract stays.
- The size ratchet and the strict-mypy precedent (#2289 item 9) apply to every new module.
