# What people use Nimbus for

Nimbus plans a home battery, solar and flexible loads against electricity
prices. These are the common reasons to run it, and where to set each one up.

## Charge when power is cheap, use it when it's dear

The core of Nimbus. On a time-of-use tariff, or a retail price that follows the
wholesale market, the plan charges the battery in cheap or solar-rich hours and
covers the house, or exports, when prices are high. It re-plans every five
minutes as prices and forecasts change.

**Set up:** [Getting started](setup-guide.md), then
[Price providers](providers/index.md) for your retailer. To make the battery
actually follow the plan, see [Going live](going-live.md).

## Sell into a price spike

On a wholesale-linked plan, a price spike can be worth more than anything the
battery would otherwise do. The **Price Spike Threshold** sets what counts as a
spike, and the **Price Spike Discharge Rate** how hard to sell into one when
armed. False spikes in AEMO's own forecast are checked before they are planned
on.

**Set up:** [Every setting](configuration-reference.md) (the price spike
numbers) and [Safety features](safety.md).

## Meet a P2P export commitment

With LocalVolts peer-to-peer export, you commit to export blocks: hours and kW.
The plan exports at least the block's rate inside each block and prices the
matched volume at the sell price plus your P2P bonus, charging beforehand so
the battery has the energy.

**Set up:** [LocalVolts](providers/localvolts.md).

## Have the EV charged by the time you leave

Add the car as a **battery participant** with a departure hour and the charge
it needs by then. The plan charges it in the cheapest hours that still meet the
target, only while the car is plugged in, and can plan around trips in a
calendar. Two cars on one charger can share its limit.

**Set up:** [Getting started](setup-guide.md), section 14 (Battery
participants).

## Run hot water, the pool pump or the aircon at the right time

Give Nimbus a **controllable load** and the device that switches it (a
`switch`, `water_heater` or `climate` entity), and it schedules that load into
cheap or solar hours, within the limits you set.

**Set up:** [Controllable loads](controllable-loads.md).

## See where the power goes, and where it's going

Nimbus learns each load you add (the whole house, a circuit, a device) from
your own history and forecasts it four days ahead. The Power Flow and Topology
cards show what each one is drawing now; the Forecaster tab shows what's
expected.

**Set up:** [Getting started](setup-guide.md), Part 2, and
[Dashboards and cards](dashboards.md).

## Check how good the plan was

Every day Nimbus scores the plan it ran against the best plan it could have
made with perfect hindsight. The Quality score and the Regret cards show what a
better forecast or setting would have been worth.

**Set up:** [Getting started](setup-guide.md), section 15 (The daily quality
score), and [Entities](entities.md).
