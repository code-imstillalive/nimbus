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
