# Vendored schemas — where they came from and how they stay honest

Files here are **copies of someone else's source of truth**. That is a
liability unless the copy carries its own provenance, which is what this
file is for.

## `telemetry.schema.json`

The `nem-flex-telemetry` record schema, v2.0. Nimbus emits records
against it (nimbus issues [#495][495] and [#496][496]); the schema itself
is Mark Purcell's, in his own companion project.

| | |
|---|---|
| upstream repo | https://github.com/purcell-lab/nem-flex-telemetry |
| upstream path | `schema/telemetry.schema.json` |
| **pinned commit** | `024f45c61b3f4a6cb251326f0b15955a9556d3ed` |
| pinned commit date | 2026-05-05 |
| pinned commit subject | `v0.3.0: schema v2.0 - assets, deferrable loads, shadow prices, $/kWh` |
| vendored on | 2026-09-17 |
| sha256 of this copy | `24478a75be3cbaae53b1ff613b911483149e8362608f83bcd29078dd3db91f35` |

Raw URL for the pinned revision (not `main` — pinning is the point):

```
https://raw.githubusercontent.com/purcell-lab/nem-flex-telemetry/024f45c61b3f4a6cb251326f0b15955a9556d3ed/schema/telemetry.schema.json
```

### Why a pinned commit rather than `main`

#495's own reasoning, and it is right: *"a one-time vendored copy will
drift the moment your `nem-flex-telemetry` schema moves, so a recorded
upstream commit ref + a periodic drift check is the honest version."*

A copy with no recorded origin is worse than no copy — it looks
authoritative and silently stops being true. The commit ref above is what
makes "has this drifted?" a question with an answer.

**Measured when vendoring, and it bears on #495's own "is v2.0 stable
enough to build against now rather than chasing a moving target":** the
file's last upstream change was **2026-05-05**, four months before it was
vendored, while the repo's own HEAD had moved as recently as the same
morning. So the schema is the settled part of an actively-developed
project, not a moving target.

### How the drift check works

`tests/test_vendored_telemetry_schema.py` holds two layers, deliberately
separate:

1. **Always-on, offline.** The vendored file's sha256 and its identity
   (draft, `version`, `additionalProperties: false`, the 23 required
   fields, and the specific enums Nimbus's emitter depends on) are pinned
   as tests. These catch a local edit — someone "fixing" a vendored file
   is exactly the failure a vendored file invites — and they never touch
   the network, so CI cannot be broken by an outage or a rate limit.

2. **Opt-in, online.** Set `NIMBUS_SCHEMA_DRIFT_CHECK=1` to additionally
   fetch upstream `main` and compare. Skipped by default. Run it when
   picking up #495/#496, or periodically; a difference means the pinned
   ref above needs re-reviewing and re-pinning, which is a deliberate act
   rather than something that should happen silently on a CI run.

The split matters: an always-on network test would make this repo's CI
depend on another repo's availability, and the failure would look like a
Nimbus problem. An entirely-offline check would never notice real drift.

[495]: https://github.com/code-imstillalive/nimbus/issues/495
[496]: https://github.com/code-imstillalive/nimbus/issues/496
