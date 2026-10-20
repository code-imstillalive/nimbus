# AEMO / NEM regional forecasts

Your retailer's own forecast usually covers a day or less. A regional spot
forecast from AEMO lets Nimbus plan past it. Nimbus adds the markup it has
learned between your retail price and the wholesale price, interval by interval.

The same forecast is the **fallback** when your retailer's prices stop
updating: the plan and the daily score are priced from it until fresh prices
return.

## Which integration

| Integration | Sensor Nimbus proposes |
|---|---|
| **AEMO NEM Data** (`aemo_nem`) | the region's *current 30-minute forecast* |
| **NEM PD7DAY** (`nem_pd7day`) | the region's days 1-7 *forecast* (not the days 2-7 continuation or a tariff sensor) |

Nimbus proposes it for **one field only**: the **regional spot forecast**
(wizard screen 3). It is a wholesale price, and Nimbus cannot see whether your
tariff passes spot through, so it never proposes it as your import or export
price. With several regions configured, the one matching your home's NEM region
is proposed; if that is not clear, nothing is.

!!! warning "An Australian NEM install without one gets a Repair"
    Without a regional forecast, prices past your retailer's horizon are held
    flat, and a stalled retailer feed has nothing to fall back to. Nimbus
    raises a Repair that names an installed forecast integration, or the one to
    install.

## The live price

The **regional spot current price** sensor (the AEMO 5-minute dispatch price)
is optional but recommended. Nimbus uses it:

- to learn your markup;
- to cross-check your retailer's live price;
- to tell a real price spike from a false one in AEMO's own forecasts (see
  [Safety features](../safety.md)).

## Your site's loss factors

AEMO publishes the **regional reference price (RRP)**. Your retail prices are
built from it with your site's **loss factors**, the marginal loss factor
(MLF) of your transmission node and your distribution loss factor (DLF):

- **Sell price** = RRP × MLF × DLF + network export tariff
- **Buy price** = (RRP × MLF × DLF + network import tariff + retailer usage
  fees) × 1.1 GST

On LocalVolts sites the RRP × MLF × DLF part matches to within 0.1 %
([#1786](https://github.com/code-imstillalive/nimbus/issues/1786)).

Two optional fields on wizard screen 2 (Grid Prices), under source 2 with
the regional spot sensors, identify them:

- **Transmission node (TNI) sensor:** a sensor whose state is your TNI, for
  example `NCMA`;
- **DLF code sensor:** a sensor whose state is your DLF code, for example
  `BL0A`.

LocalVolts v2 2.13.0 and later provides both. They are disabled by default;
enable them on the LocalVolts device page. Nimbus looks the values up in
AEMO's published tables for each financial year (2025-26 and 2026-27 so far),
so nothing is fitted, and a plan that crosses 1 July uses each year's own
values.

With both set, the AEMO-based price source on the Solver tab's Price sources
card becomes **Retail from AEMO RRP**:

- the current interval comes from the dispatch RRP, not a forecast;
- only the network tariffs and your retailer's usage fees are learned.

The plan's `price_consolidation.loss_factor` attribute shows the codes, the
financial year, the factor and, when it can't be looked up, why. This changes
only the price source the consolidation measures, not the plan's own prices.
