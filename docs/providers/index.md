# Choosing a price provider

Nimbus plans against two prices: what you pay to **import** a kWh, and what you
are paid to **export** one. Both must follow your live retail price. A price
*forecast* lets the plan look ahead; without one, Nimbus holds the current
price across the horizon.

| Your retailer | Install first | Then |
|---|---|---|
| LocalVolts | [LocalVolts v2](https://github.com/purcell-lab/localvolts_v2) | [LocalVolts](localvolts.md) |
| Amber | Amber Express, or Home Assistant's Amber Electric | [Amber](amber.md) |
| Any Australian NEM retailer, for a forecast past your retailer's own | AEMO NEM Data or NEM PD7DAY | [AEMO / NEM regional forecasts](aemo-nem.md) |
| Anything else, a flat tariff, or a time-of-use tariff | Your own price sensors or template helpers | [Any other price sensor](generic.md) |

## Nimbus finds them for you

For LocalVolts v2, Amber Express, Amber Electric, AEMO NEM Data and NEM
PD7DAY, Nimbus looks for an installed integration and **proposes** its
sensors for any Solver price field that is still empty.

- **Proposals are found by integration, not by entity name,** so a renamed
  sensor is still found.
- **They are listed under Nimbus → Configure → Review pricing suggestions.**
  Nothing changes until you submit that step.
- **A field you have set is never proposed for,** even while its sensor is
  temporarily missing.
- **More than one account or site** of the same provider is ambiguous, so
  nothing is proposed.

To switch provider later, use `select.nimbus_pricing_source` on the Nimbus
device. It lists every complete provider Nimbus finds, plus **Manual**. Picking
one changes import and export together, and only after checking that both of
its sensors exist and are reporting. See
[Switching between price providers](../setup-guide.md#switching-between-price-providers).

## Mistakes Nimbus reports

These appear under **Settings → Repairs**, and each clears itself once fixed:

- a price sensor that is frozen at forecast build (LocalVolts "Rate All Var");
- network or flat fees set on top of a price that already includes them;
- an Australian NEM install with no regional spot forecast;
- a required price field left empty.

## Combining several price sources (shadow by default)

When more than one price source is configured (for example LocalVolts plus
Amber, AEMO pre-dispatch and P5MIN), Nimbus also builds one combined forecast
from them and tracks each source's running error. By default this only runs
alongside the plan, for comparison. The switch
`switch.nimbus_solver_price_consolidation_live` makes the plan use it for
future periods. The current interval always uses its settled price.

How they are combined: in each period, an error-weighted mean of the
sources that have enough settled history (50 intervals at that lead), or
the median of all of them while none has. A source added later is scored
first and counts only once it has earned a weight, so a new source can't
move the blend before its accuracy is known. **A steady premium is not error.** A
source on another contract (a retailer that adds hedging and fees per kWh,
say) sits a steady amount above your provider. Nimbus learns each source's
average offset from settled prices, per side and lead, scores the source on
what is left, and blends it with the offset removed. The table still shows
each source's own price. **A spike is never
averaged away.** In any period where a source forecasts at or above your
*Price Spike Threshold*, the highest source is used. In a dry run, a median
that dropped a real spike only one source saw would have cost about $250
([#1786](https://github.com/code-imstillalive/nimbus/issues/1786)).

The Solver tab's **Price sources** table shows them side by side: each
source's share of the blend and how far off it has been, then the prices.
The first price column, **Now** (yellow), is the interval running now, with
each source's own price for it; once that interval's price has settled, the
Blend there is the settled price. The next intervals follow. Below the
sources, the **Blend** and its **Gap** to your provider, in every column, and
a **Learned correction** row: what the learned offsets added to the weighted
sources, so Blend = the weighted sources + the correction. With **AEMO's own
price forecast** set (Grid Prices), the retail-from-RRP row is built from
that forecast up to its last row and from the regional PD7DAY forecast after
it; its label names the switch time.
