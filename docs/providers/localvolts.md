# LocalVolts

Install the [LocalVolts v2](https://github.com/purcell-lab/localvolts_v2)
integration (2.9.0 or later) first. Nimbus then proposes these five sensors for
any of the fields that are empty (**Nimbus → Configure → Review pricing
suggestions**):

| Solver setting | Wizard screen | LocalVolts v2 sensor |
|---|---|---|
| Import price | 2 | Buy Flex Up |
| Export price | 2 | Sell Flex Up |
| Price forecast array | 3 | Flex Up Forecast |
| P2P matched rate | 3 | Current Sell Rate |
| P2P settlement history | 3 | P2P Settlement History |

## Three rules

!!! danger "Use Flex Up for prices, never Rate All Var"
    A "Rate All Var" figure is fixed when LocalVolts builds the forecast for an
    interval and is not revised when the price settles. On 9 Oct 2026 Sell Rate
    All Var stayed near 13c while Sell Flex Up and the wholesale price reached
    about $1.04. Nimbus refuses a Rate All Var sensor in the price fields and
    raises a Repair if one is already set.

!!! warning "Leave the network and flat fee rates at 0"
    Buy Flex Up already includes your network charges and LocalVolts' fees. A
    fee set in Nimbus counts them twice. Nimbus raises a Repair if fees are
    set alongside Buy Flex Up.

!!! info "Set the price forecast array when you use P2P"
    The P2P matched rate is read only through the price forecast array. With
    the matched rate set and no array, the plan shows no P2P, and Nimbus warns
    about it.

## P2P blocks

Your committed P2P export blocks (hours and kW) are set on the Solver tab's
**P2P** and **P2P hours and matching** cards. The plan exports at least the
block's rate inside each block, and prices the matched volume at the sell price
plus your P2P bonus.

## When LocalVolts stops updating

On 16 Oct 2026 LocalVolts stopped rebuilding its rows for three hours while
every poll still succeeded. Nimbus checks each row's own refresh stamp:

- **If the stamp is more than an hour old,** the plan prices from AEMO's
  forecast plus your learned markup instead
  ([#1793](https://github.com/code-imstillalive/nimbus/issues/1793)).
- **The daily score re-prices those periods the same way,**
  so a stale feed cannot distort your score
  ([#1825](https://github.com/code-imstillalive/nimbus/issues/1825)).

Both need an AEMO regional forecast to fall back to: see
[AEMO / NEM regional forecasts](aemo-nem.md).

The full walk-through, including the optional second matched-rate source, is in
[Getting started](../setup-guide.md#if-you-are-on-localvolts-install-localvolts-v2-first).
