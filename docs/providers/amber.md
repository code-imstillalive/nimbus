# Amber

Nimbus works with either Amber integration. Install one, and Nimbus proposes
its sensors for the empty import and export price fields
(**Nimbus → Configure → Review pricing suggestions**).

| Integration | Import price | Export price |
|---|---|---|
| **Amber Express** (`amber_express`) | the site's *general price* sensor | the site's *feed-in price* sensor |
| **Amber Electric**, Home Assistant's built-in (`amberelectric`) | the site's *general forecast* sensor | the site's *feed-in forecast* sensor |

For Amber Electric, Nimbus proposes the **forecast** sensors, whose forecast
rows it reads, not the current-price ones. The controlled-load price is not the
whole site's import price and is never proposed.

## Notes

- **One site only:** with more than one Amber site, nothing is proposed. Set
  the fields yourself.
- **Wholesale reference:** Amber Express also reports each interval's wholesale
  price in the same response. Nimbus uses it as the reference when it learns
  your retail markup ([#1698](https://github.com/code-imstillalive/nimbus/issues/1698)).
- **Fees:** Amber's prices already include network charges, so leave Nimbus's
  network and flat fee rates at 0.
- **Past Amber's own horizon:** add an AEMO regional forecast. See
  [AEMO / NEM regional forecasts](aemo-nem.md).
