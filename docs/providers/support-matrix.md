# Device setup: what Nimbus can find for you

When you add Nimbus, or open **Configure → Device setup → Review what Nimbus
finds**, Nimbus looks at your Energy Dashboard and at the price providers below,
and proposes the sensors to use. This page says how far each one has got.

| State | Means |
|---|---|
| **supported** | Nimbus has a profile for it, tested on fixtures, and at least one consenting household has run it. |
| **proposed** | Nimbus has a profile and fixtures, but no household has run it yet. It should work; please tell us how it goes. |
| **reported only** | Nimbus recognises it but doesn't propose it, for the reason given. You can still pick its sensors by hand. |
| **manual** | No profile: choose its sensors yourself, in the setup steps or in **Device setup → Edit a source**. |

Nothing is listed as *supported* on fixtures alone.

## Price providers

| Provider | Profile | State | Notes |
|---|---|---|---|
| LocalVolts (v2 integration) | `localvolts_v2` | proposed | Buy and sell prices, with their forecasts. The matched P2P rate and settlement history are not proposed yet. |
| Amber Express | `amber_express` | proposed | General and feed-in prices, with their forecasts. |
| Amber Electric (Home Assistant core) | `amberelectric` | proposed | General and feed-in prices, with their forecasts. |
| AEMO / NEM regional forecast | `aemo_nem` | reported only | It extends a retail price feed; the device setup has nowhere to hold a regional wholesale forecast yet. |
| NEM PD7DAY | `nem_pd7day` | reported only | It continues a retail price feed past its horizon; the device setup has nowhere to hold that yet. |
| OpenADR 3 (VEN) | `openadr3_ven` | reported only | It doesn't state its currency or whether a price is a full tariff, so it is never proposed. |
| Any other price sensor | (none) | manual | Pick it in the setup steps or in **Edit a source**. |

## Your equipment

| What | Found from | State |
|---|---|---|
| Grid connection (power, energy, prices) | Energy Dashboard | proposed |
| Solar (one device per inverter) | Energy Dashboard | proposed |
| Battery (power, energy, charge level) | Energy Dashboard | proposed; its capacity is asked once |
| Individual devices and circuits | Energy Dashboard's individual devices | proposed |
| A power sensor that matches a device by its history | history pairing | proposed, always confirmed by you |

This page is updated with each release. When your install runs a profile,
tell us on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574) so
it can move to *supported*.
