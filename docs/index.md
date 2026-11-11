# Nimbus

Nimbus is a Home Assistant integration that plans your battery, solar and
flexible loads against your electricity prices. It learns your household's
load from your own history, forecasts it, and solves a cost-minimising plan
every five minutes. Every day it also scores that plan against perfect
hindsight, so you can see how good it was.

It runs inside Home Assistant. There is no cloud service and no account.

Not sure it fits your home? See [what people use it for](use-cases.md).

## If you already run EMHASS or HAEO

Nimbus does the same job: it is an alternative to either, not an add-on to
them. All three plan a battery against prices with a linear-programming solver
and re-plan as things change. Where they differ, from the
[living comparison](https://github.com/code-imstillalive/nimbus/issues/1797)
kept up to date with every release:

- **Its own load forecast.** Nimbus learns your house's load from your own
  history, as a whole and circuit by circuit. EMHASS has a load forecaster too;
  HAEO uses a forecast you supply.
- **Uncertainty in the plan.** Separate dials for import-price, export-price
  and solar-forecast risk, using the solar forecast's own p10/p90 band.
- **Peer-to-peer export.** It can plan committed export blocks (LocalVolts P2P).
- **A daily score.** Each day's plan is scored against perfect hindsight, so
  you can see what it cost you. Neither of the others measures this.
- **Guards.** Stale prices, false price spikes, failed solves and device
  commands each have one ([Safety features](safety.md)).

Where the others are ahead: HAEO models a network topology with pricing per
connection, which Nimbus does not; EMHASS has a much larger community and a
longer track record.

You can install Nimbus beside the one you run. It only plans until you connect
it to your battery yourself, and it never reads another optimiser's plan, so
the two don't interfere.

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
