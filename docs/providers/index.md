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
