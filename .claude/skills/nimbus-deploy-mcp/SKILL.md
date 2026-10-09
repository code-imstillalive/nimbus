# Deploying Nimbus via HA MCP (HACS-managed install)

How to ship a new Nimbus release to the Home Assistant instance this session's
`Home_Assistant` MCP connection reaches — **not** NUC1/NUC2, and not the
git-symlink method CLAUDE.md's own "Deploy" section describes.

## This is a different install from CLAUDE.md's "Deploy" section — don't conflate them

CLAUDE.md documents deploying to **NUC1/NUC2**: a direct git clone symlinked
into `custom_components/nimbus_load`, deployed via `git fetch --tags && git
checkout vX.Y.Z` + `docker restart` over SSH. **This session has no path to
that at all** — confirmed directly: no SSH keys/config in this container, and
NUC1's LAN (`192.168.86.x`) is unreachable from here. There is no filesystem
or shell access to that host from a Claude Code cloud session.

The install this session's HA MCP tools actually reach is a **separate, real,
HACS-managed instance** (confirmed via `ha_get_system_health`: a real HAOS
box, Supervisor, `code-imstillalive/nimbus` shows `installed: true` under
`ha_get_hacs_info`). It has real hardware and live dispatch wired up
(`input_boolean.nimbus_live_dispatch_armed`, battery automations) — treat it
with the same care as any live-dispatch install, but it updates through HACS,
not git+docker.

**The mistake this corrects:** a session assumed "the live HA install this
session can reach" meant "NUC1, therefore git+docker, therefore I can't do
this myself" and said so to the household — who then had to correct it
("This isn't nuc1, please deploy with MCP via hacs"). Check which install you
actually have before reaching for CLAUDE.md's NUC1 recipe: `ha_get_hacs_info`
showing `installed: true` for the nimbus repo is the tell that this skill
applies, not that one.

## Steps

1. **Check current vs. available version:**
   ```
   ha_get_hacs_info(action="info", repository_id="code-imstillalive/nimbus")
   ```
   Read `installed_version`, `available_version`, `pending_update`. Don't
   trust a single entity's `nimbus_version` attribute for "what's installed
   now" — HACS's own record is the direct answer to that question.

2. **Confirm the target is a real validated release**, not just a pushed tag —
   per nimbus #1447/#1664, a tag can exist without a published GitHub Release
   if the validation gate refused it. `mcp__github__get_latest_release` (or
   `get_release_by_tag`) returning the release confirms it actually published.

3. **Check the timing/safety window before restarting**, if this install runs
   live dispatch. A full HA restart drops the active automation loop for the
   duration. Check:
   - the time of day against any known dispatch-sensitive window (e.g. this
     install's P2P export blocks run ~21:00–24:00 — restarting inside one
     interrupts a live export commitment);
   - whether a battery/grid sensor shows an active commitment right now.

   If you're not confident it's safe, say so and ask before restarting, the
   same as any other hard-to-reverse live action.

4. **Deploy:**
   ```
   ha_manage_hacs(action="download", repository_id="code-imstillalive/nimbus", version="vX.Y.Z")
   ```
   This downloads the files only. A custom integration needs a restart to
   activate — the tool's own response says so.

5. **Pre-restart sanity check:**
   ```
   ha_get_system_health(include="config_check")
   ```
   Confirm `config_check.result == "valid"` before restarting.

6. **Restart:**
   ```
   ha_restart(confirm=True)
   ```
   **Expect the call itself to error** (a 502/connection-reset is normal —
   the request to trigger the restart lands, then HA's web server drops
   mid-restart before it can respond). Don't read that as a failed restart;
   it's what triggering a restart you can't watch finish looks like.

7. **Wait for the restart to complete.** This session has no network path to
   poll HA directly (same LAN-unreachability as the NUC1 case above), and the
   HA MCP tools themselves are unreachable while HA is down — so there's no
   condition to poll, only a bounded wait. A full HAOS + Supervisor + add-on
   stack restart on an install with this many add-ons typically takes
   60–180 s. Use a single bounded background wait (`Bash` with
   `run_in_background: true`, a plain `sleep N`), not a polling loop — there
   is nothing reachable to poll.

8. **Verify, using a histogram, not one sensor** (CLAUDE.md's own standing
   rule for exactly this reason — one sensor can lag behind the rest):
   - `ha_get_hacs_info(action="info", ...)`: `installed_version` now matches
     the target, `pending_update: false`.
   - Read `nimbus_version` off 3+ different Nimbus entities
     (e.g. `sensor.nimbus_solver_current_import_price`,
     `sensor.nimbus_solver_lp_status`, `sensor.nimbus_flex_telemetry`) — all
     should agree on the new version. A stale one that hasn't republished yet
     is a timing artifact, not a failed deploy, as long as the others agree.
   - `ha_get_integration(domain="nimbus_load")`: `state == "loaded"`.
   - `ha_get_logs(source="system", hours_back=1)`: no new ERROR/Traceback
     tied to `nimbus_load` since the restart.
   - Confirm a live solve: `sensor.nimbus_solver_lp_status` reads `optimal`
     after the restart (check its own `last_updated`, not just its state, to
     confirm it's a fresh solve and not a stale pre-restart reading).

9. **Report what was actually checked**, the same discipline as any release
   validation in this repo — which checks passed, which entity readings you
   used for the version histogram, and whether anything in the log needs
   following up. "Deployed" without having read any of the above back is not
   a validated deploy.
