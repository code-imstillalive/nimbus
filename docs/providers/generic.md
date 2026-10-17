# Any other price sensor

Any Home Assistant sensor whose state is a price in **$/kWh** can be the import
or export price. Set them on Solver settings screen 2.

- **A sensor that follows your live price:** use it directly. If it carries a
  price forecast in a format Nimbus reads, the plan uses that forecast.
  Otherwise the current price is held across the horizon.
- **A flat tariff:** create two template helpers, as shown in
  [Create price sensors for a flat tariff](../setup-guide.md#create-price-sensors-for-a-flat-tariff).
  Use per-kWh rates only (not the daily supply charge). Keep a negative export
  price's sign.
- **Network fees by time of day:** leave the price sensor as the energy price
  and set the default network fee and up to three time blocks on the Solver
  tab's **Grid and fees** card. Buy = price + network fee + flat fee.
- **OpenADR 3 (VEN):** Nimbus lists any OpenADR price sensors it finds but does
  not fill them in. OpenADR does not say whether a price is a full tariff or an
  incentive added to one.

!!! tip "Check the plan's prices"
    After setup, compare the prices on the Solver tab with your bill or app.
    Do not assume a forecast was accepted: check that the plan's prices change
    across the day if your tariff does.
