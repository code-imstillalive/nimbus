# Nimbus

Nimbus is a Home Assistant integration that plans your battery, solar and
flexible loads against your electricity prices. It learns your household's
load from your own history, forecasts it, and solves a cost-minimising plan
every five minutes. Every day it also scores that plan against perfect
hindsight, so you can see how good it was.

It runs inside Home Assistant. There is no cloud service and no account.

## Where to start

1. **[Getting started](setup-guide.md)**: install with HACS, add the
   integration, run the Solver settings wizard, and check that it works.
   Part 1 is the basic setup and Part 2 the advanced one.
2. **[Price providers](providers/index.md)**: which sensors to use for
   LocalVolts, Amber, AEMO regional forecasts or any other price source.
3. **[Dashboard tabs](tabs.md)**: what each tab of the Nimbus dashboard shows,
   and what each one needs before it stops being blank.

!!! warning "A basic install leaves some tabs blank"
    The basic setup is enough for a plan. The Forecaster and Topology tabs, and
    the daily score, need the extra steps in [Getting started](setup-guide.md)
    Part 2. Inside Home Assistant, the Nimbus dashboard's **Help** tab has the
    same checklist, with a live tick for each step done on your install.

## What it does and does not control

Nimbus plans. It only acts where you connect it: a battery, through your own
automation that follows the plan, and the controllable loads you give it a
device to command. See
[What Nimbus does and does not control](setup-guide.md#what-nimbus-does-and-does-not-control).

## Safety

Stale prices, false price spikes, failed solves and device commands each have
a guard. See [Safety features](safety.md).

## Getting help

- **Report a problem:** [GitHub issues](https://github.com/code-imstillalive/nimbus/issues).
  Include **Download diagnostics** from the Nimbus integration page.
- **Something not working:** [Troubleshooting](troubleshooting.md).
- **Testing Nimbus:** [Testers](TESTERS.md).

Nimbus is open source (MIT). The code, releases and changelog are on
[GitHub](https://github.com/code-imstillalive/nimbus).
