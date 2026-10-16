# 2026-10-16: calibrated solar p10 / p90 (#1807)

#1805's measurement found the solar band was an envelope, not a quantile band:
`lower_kw` 0 all day, `upper_kw` 6-17 kW at midnight. This adds calibrated
`SolarConfig.p10_kw` / `p90_kw` and leaves the envelope alone.

## Measured first (read-only)

- **Actual:** production's recorder, `sensor.combined_total_dc_power`, read
  with GET.
- **Solcast's p10/p50/p90:** devhub's recorder, from the mirrored
  `detailedForecast`. Production keeps no Solcast history.
- **The blended p50 at every lead:** devhub's `export_decision_inputs`
  horizons, 10-16 Oct. Devhub's solar config matches production's.

Coverage below p10 / above p90 (target ~10 / ~10). The empirical rows are
leave-one-day-out:

- Solcast's own band: 9.2 / 29.6. Its p90 is far too low on this install.
- Blend x Solcast ratios: 17.4 / 22.7.
- Solcast absolute, widened to contain the blend: 14.0 / 16.6. This is the
  fallback.
- Empirical by lead: 11.3 / 11.8. That looks calibrated, but by p50 size it
  is 3.8 / 7.4 at >= 2 kW and 26.9 / 21.0 at < 2 kW, so midday p10 sat near 0.
- **Empirical by lead x p50 size: 11.5 / 12.7 (11.7 / 13.4 and 10.9 / 11.5
  by size), adopted.**
- By hour of day it was worse (14.2 / 14.0 by 3-hour block, 17.3 / 16.0 by
  hour). A week is too few days per cell.

The shipped module, replayed on the same data with its sparser one sample per
interval and bucket, scores 11.6 / 12.8 out of sample.

## Not done

- **#1805 is not edited.** It needs to prefer `p10_kw` / `p90_kw`, and to null
  them in its three `replace()` calls (see the PR).
- **No devhub validation.** The history needs days to fill, and devhub's
  solve-output entities are squatted by production's mirror.
- **The standalone/cron copy is not ported.** It is registered as known open
  drift, like #1784.
