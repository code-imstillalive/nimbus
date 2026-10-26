# Troubleshooting

## First, look in Repairs

**Settings → Repairs** lists the setup problems Nimbus can detect. Each entry
says what is wrong and where to fix it, and clears itself once fixed:

- untrained forecasts;
- wrong units;
- doubled fees;
- frozen or stale price inputs;
- missing inputs;
- placeholder battery numbers;
- a battery participant whose "States that mean available" matched none of
  the states its availability entity reported in the last 7 days.

## Common problems

The full list, with fixes, is in
[Getting started → Troubleshooting](setup-guide.md#troubleshooting):

- [the Solver config is not `configured`](setup-guide.md#sensornimbus_solver_config-is-not-configured);
- [the plan status is not `optimal`](setup-guide.md#sensornimbus_solver_lp_status-is-not-optimal);
- [the plan looks wrong](setup-guide.md#the-plan-looks-wrong-nonsensical);
- [a forecast reads `unknown`](setup-guide.md#a-forecast-sensor-reads-unknown);
- [a controllable load is not being switched](setup-guide.md#a-controllable-load-is-not-being-switched);
- [the dashboard cards do not render](setup-guide.md#the-dashboard-cards-do-not-render);
- [the quality score is missing or looks impossible](setup-guide.md#the-quality-score-is-missing-or-impossible-looking).

## Check everything at once

[Getting started § 8](setup-guide.md#a-paste-ready-template-check) has one
template to paste into **Developer Tools → Template**. It checks the plan, its
inputs and their coverage in one go.

## Reporting a problem

1. On the Nimbus integration page, open **⋮ → Download diagnostics**.
2. Open an issue on
   [GitHub](https://github.com/code-imstillalive/nimbus/issues/new) with your
   Nimbus and Home Assistant versions, what you did, what you expected and what
   happened.
3. Attach the diagnostics **after reading them**: remove tokens, addresses and
   anything private.

See [Testers](TESTERS.md) for what makes a report easy to act on.
