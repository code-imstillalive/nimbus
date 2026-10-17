# Dashboard tabs

Nimbus adds a **Nimbus** dashboard on a new install and keeps its tabs up to
date. A tab you have edited is never overwritten. When a release redesigns it,
the new design is added beside yours.

| Tab | What it shows | What it needs before it is not blank |
|---|---|---|
| **Forecaster** | Your load forecast against what really happened, with its confidence band. | A whole-house Power Signal (and optionally Loads), plus the **ApexCharts Card** from HACS → Frontend. |
| **Solver** | The plan: money, battery, prices, the day-by-day summary, and every setting with its live value. | Solver settings (screens 1-3) and your real battery and grid numbers. |
| **Topology** | A diagram of your switchboard, inverters, PV strings, batteries and loads. | Power Sources, PV strings and battery towers added on the integration page. |
| **Control Panel** | What the battery is doing now and next, with the plan's reasons. | Solver settings. |
| **Regret** | Yesterday scored against perfect hindsight: EPR, regret and where they came from. | One full day of real load, solar and battery data. Scored each morning. |
| **Help** | Setup steps with a live tick for each, where everything is, every setting with its formula, a glossary and what's new. | Nothing. |

!!! tip "The Help tab"
    Inside Home Assistant, the **Help** tab is the most complete reference for
    your own install. Its ticks show which steps are done, and it lists every
    Solver-tab setting with its location and formula.

More on each card, and how to build your own dashboard instead:
[Dashboards and cards](dashboards.md). Every setting, field by field:
[Every setting](configuration-reference.md).
