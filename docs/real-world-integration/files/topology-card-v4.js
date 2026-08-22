/**
 * Topology Card — a real SVG power-flow diagram: Switchboard, 2 Inverters
 * (each with PV strings + battery towers), auto-discovered Loads, whole-
 * house readout.
 *
 * v7, 2026-08-20 -- Loads are no longer a hand-maintained list in
 * topology_map.yaml. Every Nimbus "load" subentry (HWS, pool, an
 * individual circuit breaker -- anything added via the Nimbus hub's own
 * "+ Add" -> Load UI) auto-publishes a forecast sensor named
 * sensor.nimbus_<source_object_id>_forecast, deterministically derived
 * from its own real source sensor's entity_id (see nimbus's own
 * sensor.py:_object_id_from_source -- e.g. sensor.cb_pw_l1_power ->
 * sensor.nimbus_cb_pw_l1_power_forecast) and carries a `subentry_type`
 * attribute distinguishing "load" from "power_signal". _discoverLoads()
 * below scans hass.states for exactly these at EVERY render (no config
 * needed, no redeploy needed) -- add a load in the Nimbus UI, it appears
 * on this card on the very next hass update. "power_signal" subentries
 * (Battery/Solar/Grid/Whole House) are deliberately excluded here --
 * those are already shown elsewhere on this same diagram (the inverter
 * bars, the switchboard bus, the whole-house headline), duplicating them
 * as generic "load" tiles too would be wrong, not just redundant.
 *
 * v6, per direct design feedback:
 * - Every "is this flowing" check now normalizes to watts first
 *   (toWatts()) instead of comparing a raw number against a threshold --
 *   a real bug: sensor.logger_meter_total_active_power reports in kW, so
 *   a genuine 0.6kW/600W reading was failing a ">5" check meant for watts
 *   and showing as idle/no-arrow.
 * - Each inverter is now a tall vertical bar (a smaller, fatter version
 *   of the switchboard bus itself, same visual language) instead of a
 *   small horizontal box. PV strings tap its LEFT side via a perfectly
 *   straight horizontal line at the string's own height (no diagonal
 *   fan-in). The battery chain taps its BOTTOM via a single straight
 *   vertical drop, then chains horizontally -- never a diagonal.
 * - Loads are back to a single column, but each one now gets ONE direct
 *   line straight off the main switchboard bus itself (the bus is
 *   extended to span the full load-list height) -- no intermediate
 *   spine/trunk system at all, since "each is directly wired to the
 *   switchboard" in reality.
 * - Added real daily kWh headlines (whole-house daily load, daily solar
 *   generation, daily battery charge/discharge) sourced from the same
 *   entities already confirmed live on this system's own working
 *   sunsynk-power-flow-card / power-flow-card-plus configs -- not
 *   guessed.
 *
 * Deliberately vanilla JS + hand-laid-out SVG, no framework/build step --
 * a plain file HA serves directly from /local/. Config shape mirrors
 * config/topology_map.yaml exactly.
 *
 * Registration (one-time, manual):
 *   Settings -> Dashboards -> (three dots) -> Resources -> Add Resource
 *   URL: /local/topology-card-v4.js   Type: JavaScript Module
 */

const NS = "http://www.w3.org/2000/svg";

const COLORS = {
  gridNode: "#d8d8d8",
  gridSell: "#3ac6e8",
  gridImport: "#e0524a",
  solar: "#f0a839",
  inverter: "#c9d94a",
  battery: "#4ac9e0",
  batteryLow: "#e05a5a",
  load: "#c874d1",
  bus: "#d8d8d8",
  unavailable: "#454549",
  line: "#454549",
  lineActive: "#f0a839",
  text: "#f2f2f2",
  textDim: "#8a8a8a",
  wh: "#ffd23f",
  // Dedicated live/health indicator (2026-08-16 ask: "green dots on all
  // entities for load so we know they are live... if they go rogue or
  // unavailable or unknown they should turn red") -- deliberately separate
  // from `unavailable` above, which greys out a whole tile's own
  // border/fill (an idle/inactive visual), not an alarm color. This pair
  // is a clean 2-state signal (is the underlying real sensor answering at
  // all right now?) fully decoupled from active/idle, purple/dim, etc.
  healthy: "#3ecf5c",
  unhealthy: "#e0524a",
  // A third real state, distinct from both (2026-08-17, direct ask: "the
  // warning would appear in the topology card... green/red dot?"): the
  // RAW circuit sensor can be perfectly healthy while that same load's
  // own Nimbus FORECAST entity failed to fetch during the Solver's last
  // solve (see nimbus_solver_forecast_writer.py's own fetch_load_
  // forecast_safe() -- individually guarded, defaults to 0.0 rather than
  // crash, but that failure is real and worth surfacing here, not just
  // silently absorbed). Amber, not red -- a stale/missing FORECAST is a
  // real but lesser concern than the raw sensor itself being down.
  forecastWarning: "#e0a83a",
};

// A faint, low-opacity tint over an active node's own dark background --
// full-strength color was reserved for the border/icon/value text, so an
// active node looked identical to an idle one except for a thin outline.
// This adds a second, translucent rect on top of the base fill only when
// active, same shape/corner-radius, no change to any existing element.
// Maps HA's own standard weather condition strings (what any weather.*
// entity's state literally is -- confirmed live against weather.pirateweather
// returning "clear-night") to one of this card's own basic line-art icon
// kinds. Deliberately coarse ("basic" was the ask) -- every condition HA
// defines lands on one of 4 shapes, not a bespoke icon per condition.
const WEATHER_ICON_KIND = {
  "sunny": "sun", "clear-day": "sun", "clear": "sun",
  "clear-night": "moon",
  "partlycloudy": "cloud", "cloudy": "cloud", "fog": "cloud",
  "windy": "cloud", "windy-variant": "cloud", "exceptional": "cloud",
  "rainy": "rain", "pouring": "rain", "lightning": "rain",
  "lightning-rainy": "rain", "hail": "rain", "snowy": "rain", "snowy-rainy": "rain",
};

function fillOverlay(x, y, w, h, rx, color) {
  return svgEl("rect", { x: x - w / 2, y: y - h / 2, width: w, height: h, rx, fill: color, opacity: 0.12 });
}

function svgEl(tag, attrs, children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== undefined && v !== null) el.setAttribute(k, v);
  }
  for (const child of children || []) el.appendChild(child);
  return el;
}

function numOf(state) {
  if (!state || state.state === "unavailable" || state.state === "unknown") return NaN;
  return parseFloat(state.state);
}

// Normalizes a power reading to watts before any magnitude/threshold
// comparison -- entities on this system report power in either W or kW
// depending on the sensor, and a raw-number threshold silently breaks
// for whichever unit it wasn't tuned for (confirmed live: a real 0.6kW
// grid reading failed a ">5" check meant for watts).
function toWatts(state) {
  const n = numOf(state);
  if (Number.isNaN(n)) return NaN;
  const unit = ((state.attributes && state.attributes.unit_of_measurement) || "").toLowerCase();
  return unit === "kw" ? n * 1000 : n;
}

function fmtValue(state) {
  if (!state || state.state === "unavailable" || state.state === "unknown") {
    return { text: "—", healthy: false };
  }
  const num = parseFloat(state.state);
  if (Number.isNaN(num)) return { text: state.state, healthy: true };
  const unit = state.attributes && state.attributes.unit_of_measurement;
  return { text: `${num.toFixed(Math.abs(num) >= 100 ? 0 : 1)}${unit ? " " + unit : ""}`, healthy: true };
}

// Small refined line-art icons -- deliberately NOT emoji.
function icon(kind, cx, cy, size, color) {
  const s = size;
  const g = svgEl("g", { stroke: color, fill: "none", "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" });
  if (kind === "bolt") {
    g.appendChild(svgEl("path", {
      d: `M ${cx + s * 0.12} ${cy - s} L ${cx - s * 0.5} ${cy + s * 0.15} L ${cx - s * 0.05} ${cy + s * 0.15} L ${cx - s * 0.12} ${cy + s} L ${cx + s * 0.5} ${cy - s * 0.15} L ${cx + s * 0.05} ${cy - s * 0.15} Z`,
      fill: color, stroke: "none",
    }));
  } else if (kind === "sun") {
    g.appendChild(svgEl("circle", { cx, cy, r: s * 0.48 }));
    for (let a = 0; a < 8; a++) {
      const ang = (a / 8) * Math.PI * 2;
      g.appendChild(svgEl("line", {
        x1: cx + Math.cos(ang) * s * 0.68, y1: cy + Math.sin(ang) * s * 0.68,
        x2: cx + Math.cos(ang) * s * 0.98, y2: cy + Math.sin(ang) * s * 0.98,
      }));
    }
  } else if (kind === "battery") {
    g.appendChild(svgEl("rect", { x: cx - s * 0.6, y: cy - s * 0.42, width: s * 1.05, height: s * 0.84, rx: 2 }));
    g.appendChild(svgEl("rect", { x: cx + s * 0.45, y: cy - s * 0.16, width: s * 0.16, height: s * 0.32, fill: color, stroke: "none" }));
  } else if (kind === "house") {
    g.appendChild(svgEl("path", { d: `M ${cx - s} ${cy + s * 0.15} L ${cx} ${cy - s * 0.85} L ${cx + s} ${cy + s * 0.15}` }));
    g.appendChild(svgEl("rect", { x: cx - s * 0.62, y: cy + s * 0.1, width: s * 1.24, height: s * 0.75 }));
  } else if (kind === "moon") {
    // Classic crescent trick: a full circle with a second, offset circle
    // cut out of it via a "hole" path (even-odd fill), stroke-only outline.
    g.appendChild(svgEl("path", {
      d: `M ${cx - s * 0.15} ${cy - s * 0.6}
          A ${s * 0.6} ${s * 0.6} 0 1 0 ${cx - s * 0.15} ${cy + s * 0.6}
          A ${s * 0.45} ${s * 0.45} 0 1 1 ${cx - s * 0.15} ${cy - s * 0.6} Z`,
    }));
  } else if (kind === "cloud") {
    g.appendChild(svgEl("circle", { cx: cx - s * 0.35, cy: cy + s * 0.1, r: s * 0.38 }));
    g.appendChild(svgEl("circle", { cx: cx + s * 0.05, cy: cy - s * 0.12, r: s * 0.48 }));
    g.appendChild(svgEl("circle", { cx: cx + s * 0.48, cy: cy + s * 0.12, r: s * 0.36 }));
    g.appendChild(svgEl("path", {
      d: `M ${cx - s * 0.7} ${cy + s * 0.42} L ${cx + s * 0.85} ${cy + s * 0.42}`,
    }));
  } else if (kind === "rain") {
    g.appendChild(svgEl("circle", { cx: cx - s * 0.35, cy: cy - s * 0.15, r: s * 0.32 }));
    g.appendChild(svgEl("circle", { cx: cx + s * 0.05, cy: cy - s * 0.35, r: s * 0.4 }));
    g.appendChild(svgEl("circle", { cx: cx + s * 0.45, cy: cy - s * 0.15, r: s * 0.3 }));
    g.appendChild(svgEl("path", { d: `M ${cx - s * 0.65} ${cy + s * 0.1} L ${cx + s * 0.75} ${cy + s * 0.1}` }));
    [-0.4, 0, 0.4].forEach((dx) => {
      g.appendChild(svgEl("line", {
        x1: cx + dx * s, y1: cy + s * 0.35, x2: cx + dx * s - s * 0.12, y2: cy + s * 0.7,
      }));
    });
  }
  return g;
}

// Real, deterministic inverse of Nimbus's own sensor.py:_object_id_from_source
// -- that function turns "sensor.cb_pw_l1_power" into
// "sensor.nimbus_cb_pw_l1_power_forecast" by string concatenation alone
// (no HA name/slug auto-derivation involved, unlike the friendly-name ->
// entity_id gotcha documented elsewhere in this project's CLAUDE.md), so
// reversing it here is exact and safe, not a guess. Returns null for
// anything that doesn't match the expected shape.
function liveEntityFromForecast(forecastEntityId) {
  const m = /^sensor\.nimbus_(.+)_forecast$/.exec(forecastEntityId);
  return m ? `sensor.${m[1]}` : null;
}

class TopologyCard extends HTMLElement {
  setConfig(config) {
    if (!config.switchboard || !config.inverters) {
      throw new Error("switchboard-topology-card: config needs switchboard, inverters");
    }
    this._config = config;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._buildStaticShell();
  }

  // Reads Nimbus's own REAL, live version off any of its devices'
  // sw_version (2026-08-20 -- Nimbus's own sensor.py now sets this from
  // its manifest.json via async_get_integration(), single source of
  // truth, no more hand-syncing a version string here). hass.devices /
  // hass.entities are standard, well-established fields on the frontend
  // hass object (same class of API as hass.states, already used
  // extensively elsewhere in this file) -- but this is the one place this
  // card reads them, so it's kept defensive: any missing piece (an older
  // HA frontend without these fields, a Nimbus version that predates
  // sw_version, no Nimbus devices yet) returns null and the caller falls
  // back to the static topology_map.yaml value instead of breaking.
  _discoverNimbusVersion() {
    const hass = this._hass;
    if (!hass.entities || !hass.devices) return null;
    for (const entityId in hass.states) {
      if (!entityId.startsWith("sensor.nimbus_") || !entityId.endsWith("_forecast")) continue;
      const entReg = hass.entities[entityId];
      const device = entReg && entReg.device_id ? hass.devices[entReg.device_id] : undefined;
      if (device && device.sw_version) return device.sw_version;
    }
    return null;
  }

  // Auto-discovers every Nimbus LOAD (not power_signal) subentry's
  // forecast sensor from live hass state, deterministically derives its
  // real live/measured sibling entity, and derives a display name from
  // the entity's own resolved friendly_name (composed by HA as "<device
  // name> Forecast" for these entities -- stripping the fixed " Forecast"
  // suffix recovers exactly the device's own title, e.g. "CB-PW HWS L1",
  // the same name the household already sees on that load's own device
  // page). Sorted by name for a stable, jitter-free row order across
  // renders -- hass.states key order isn't a guaranteed stable sort.
  _discoverLoads() {
    const states = this._hass.states;
    const loads = [];
    for (const entityId in states) {
      if (!entityId.startsWith("sensor.nimbus_") || !entityId.endsWith("_forecast")) continue;
      const state = states[entityId];
      const attrs = state.attributes || {};
      if (attrs.subentry_type !== "load") continue;
      const live = liveEntityFromForecast(entityId);
      if (!live) continue;
      const friendly = attrs.friendly_name || entityId;
      const name = friendly.endsWith(" Forecast") ? friendly.slice(0, -" Forecast".length) : friendly;
      loads.push({ name, forecast: entityId, live });
    }
    loads.sort((a, b) => a.name.localeCompare(b.name));
    return loads;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 20;
  }

  _buildStaticShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        /* 2026-08-20 mobile fix: overflow-x lets the diagram scroll/pan
           horizontally on a narrow screen instead of the browser having
           nowhere to put the overflow -- paired with the SVG's own
           per-render min-width below (see _render()'s "totalW" usage),
           this is what actually stops mobile from shrinking. */
        ha-card { padding: 12px; background: #101013; overflow-x: auto; }
        /* width: 100% still stretches the diagram to fill a WIDE
           container exactly as before (the desktop "fill the whole page"
           panel view is untouched) -- min-width is set dynamically in JS,
           per render, to this diagram's own true native pixel width, so
           on a container NARROWER than that (a phone) the SVG can no
           longer be squeezed down past the point of being legible; the
           card scrolls instead of shrinking labels/values into unreadable
           text. */
        svg { width: 100%; height: auto; display: block; }
        text { font-family: var(--paper-font-body1_-_font-family, sans-serif); }
        .node-label { font-size: 7.5px; font-weight: 500; }
        .node-value-big { font-size: 10.5px; font-weight: 600; }
        .node-value-sub { font-size: 6.5px; font-weight: 400; }
        .wh-value { font-size: 17px; font-weight: 700; }
        .wh-label { font-size: 7px; font-weight: 500; letter-spacing: 0.3px; }
        .stat-value { font-size: 13px; font-weight: 700; }
        .stat-label { font-size: 7px; font-weight: 500; letter-spacing: 0.2px; }
        .flow-line { stroke-dasharray: 7 6; animation: flow 0.9s linear infinite; }
        @keyframes flow { to { stroke-dashoffset: -13; } }
        .glow { filter: url(#glow); }
      </style>
      <ha-card>
        <svg id="svg" viewBox="0 0 1600 800" xmlns="${NS}"></svg>
      </ha-card>
    `;
    this._svg = this.shadowRoot.getElementById("svg");
  }

  _entityState(entityId) {
    return this._hass && entityId ? this._hass.states[entityId] : undefined;
  }

  // Thin outlined box: small vector icon + label top row, big value
  // centered, optional extra sub-lines stacked at the bottom.
  _box(x, y, w, h, opts) {
    const { iconKind, label, valueText, subLines, color, healthy, active } = opts;
    const strokeColor = healthy === false ? COLORS.unavailable : color;
    const group = svgEl("g", {});
    group.appendChild(svgEl("rect", {
      x: x - w / 2, y: y - h / 2, width: w, height: h, rx: 10,
      fill: "#161619", stroke: strokeColor, "stroke-width": 1.8,
      class: active && healthy !== false ? "glow" : "",
    }));
    if (active && healthy !== false) group.appendChild(fillOverlay(x, y, w, h, 10, strokeColor));
    if (label) {
      const iconSize = 6.5;
      const lx = iconKind ? x - w / 2 + 20 : x - w / 2 + 9;
      if (iconKind) {
        group.appendChild(icon(iconKind, x - w / 2 + 11, y - h / 2 + 12, iconSize, strokeColor));
      }
      const labelEl = svgEl("text", { x: lx, y: y - h / 2 + 14, class: "node-label", style: `fill:${COLORS.textDim}` });
      labelEl.textContent = label;
      group.appendChild(labelEl);
    }
    if (valueText) {
      // Was a fixed +30 -- at h=30 (PV strings, shrunk from a taller
      // original during an earlier padding pass without rescaling this)
      // that lands exactly on the box's own bottom edge, zero margin,
      // crashing into whatever sits below. A purely h-proportional offset
      // (tried first) broke the OTHER direction instead -- the label above
      // sits at a fixed +14 regardless of h, so scaling the value's offset
      // by h alone shrinks the label-to-value gap on a short box and the
      // two started overlapping. +26 (down from 30) is verified to clear
      // the bottom edge with real margin at h=30 (PV strings) while still
      // sitting comfortably below the label, and shifts the taller Grid
      // box (h=68) up 4px with no ill effect there.
      const valueEl = svgEl("text", {
        x, y: y - h / 2 + 26, "text-anchor": "middle",
        class: "node-value-big", style: `fill:${strokeColor}`,
      });
      valueEl.textContent = valueText;
      group.appendChild(valueEl);
    }
    (subLines || []).forEach((line, i) => {
      const subEl = svgEl("text", {
        x, y: y - h / 2 + 44 + i * 11, "text-anchor": "middle",
        class: "node-value-sub", style: `fill:${COLORS.textDim}`,
      });
      subEl.textContent = line;
      group.appendChild(subEl);
    });
    return { group, cx: x, cy: y, w, h, healthy: healthy !== false, active: !!active };
  }

  _batteryBox(x, y, w, h, tower, active) {
    const socState = this._entityState(`sensor.${tower}_soc`);
    const sohState = this._entityState(`sensor.${tower}_soh`);
    const vState = this._entityState(`sensor.${tower}_voltage`);
    const tState = this._entityState(`sensor.${tower}_temperature`);
    const soc = numOf(socState);
    const healthy = !!(socState && socState.state !== "unavailable" && socState.state !== "unknown");
    // Same fix as loads/PV strings: a healthy-but-currently-idle tower (its
    // own parent inverter not actively charging/discharging right now) was
    // rendering in full battery-blue, indistinguishable from one genuinely
    // flowing -- `active` is passed down from the per-inverter real signal.
    const color = !healthy ? COLORS.unavailable : active ? COLORS.battery : COLORS.textDim;
    const label = tower.replace("battery_tower_", "Tower ");

    const group = svgEl("g", {});
    group.appendChild(svgEl("rect", {
      x: x - w / 2, y: y - h / 2, width: w, height: h, rx: 10,
      fill: "#161619", stroke: color, "stroke-width": 1.8,
      class: healthy ? "glow" : "",
    }));
    if (active && healthy) group.appendChild(fillOverlay(x, y, w, h, 10, color));
    group.appendChild(icon("battery", x - w / 2 + 11, y - h / 2 + 12, 6.5, color));
    const labelEl = svgEl("text", { x: x - w / 2 + 20, y: y - h / 2 + 14, class: "node-label", style: `fill:${COLORS.textDim}` });
    labelEl.textContent = label;
    group.appendChild(labelEl);

    const barX = x - w / 2 + 9, barY = y - h / 2 + 21, barW = w - 18, barH = 8;
    group.appendChild(svgEl("rect", { x: barX, y: barY, width: barW, height: barH, rx: 4, fill: "#0c0c0e" }));
    if (!Number.isNaN(soc)) {
      const fillW = Math.max(2, Math.min(barW, (soc / 100) * barW));
      group.appendChild(svgEl("rect", { x: barX, y: barY, width: fillW, height: barH, rx: 4, fill: soc > 20 ? COLORS.battery : COLORS.batteryLow }));
    }
    // Was +13 -- only 4px between this baseline and the stats line below
    // (y+h/2-6), not enough clearance given each line's own font ascent/
    // descent; the SOC% number was visually crashing into "SOH .. V .. °C".
    const socEl = svgEl("text", { x, y: barY + barH + 9, "text-anchor": "middle", class: "node-value-big", style: `fill:${color}` });
    socEl.textContent = Number.isNaN(soc) ? "—" : `${soc.toFixed(0)}%`;
    group.appendChild(socEl);

    const stats = [
      sohState ? `SOH ${numOf(sohState).toFixed(0)}%` : "SOH —",
      vState ? `${numOf(vState).toFixed(0)}V` : "—V",
      tState ? `${numOf(tState).toFixed(0)}°C` : "—°C",
    ];
    const statEl = svgEl("text", { x, y: y + h / 2 - 6, "text-anchor": "middle", class: "node-value-sub", style: `fill:${COLORS.textDim}` });
    statEl.textContent = stats.join("  ·  ");
    group.appendChild(statEl);

    return { group, cx: x, cy: y, healthy };
  }

  _loadTile(x, y, w, h, load, failedForecasts) {
    const liveState = this._entityState(load.live);
    const fcState = this._entityState(load.forecast);
    const live = fmtValue(liveState);
    const fc = fmtValue(fcState);
    const liveW = toWatts(liveState);
    const active = !Number.isNaN(liveW) && Math.abs(liveW) > 5;
    // Purple means "actually drawing power right now" -- a healthy-but-idle
    // load (a very normal state, most loads are idle most of the time) was
    // rendering in the same full purple as an active one, which read as
    // "18 things are all drawing power" at a glance when almost none were.
    const color = !live.healthy ? COLORS.unavailable : active ? COLORS.load : COLORS.textDim;
    // Real, distinct third state: the raw circuit is fine, but THIS
    // load's own Nimbus forecast failed during the Solver's last solve
    // (see COLORS.forecastWarning's own comment) -- checked against the
    // real failed_load_entities list published by nimbus_solver_
    // forecast_writer.py, not re-derived here.
    const forecastFailed = !!(failedForecasts && load.forecast && failedForecasts.has(load.forecast));

    const group = svgEl("g", {});
    group.appendChild(svgEl("rect", {
      x: x - w / 2, y: y - h / 2, width: w, height: h, rx: 7,
      fill: "#161619", stroke: color, "stroke-width": active ? 1.8 : 1.2,
      opacity: active ? 1 : 0.5,
      class: active && live.healthy ? "glow" : "",
    }));
    if (active && live.healthy) group.appendChild(fillOverlay(x, y, w, h, 7, color));
    // Dedicated live/health dot -- green whenever the real underlying
    // sensor is answering at all (regardless of active/idle), red the
    // instant it goes unavailable/unknown/missing, amber when the raw
    // sensor is fine but this load's own Nimbus FORECAST failed during
    // the Solver's last solve (a real, distinct, lesser concern -- see
    // COLORS.forecastWarning's own comment). Deliberately separate from
    // `color` above (which encodes active-vs-idle-vs-broken as one 3-way
    // signal for the tile's own border/fill) -- this dot answers a
    // different, narrower question on purpose: "is this live right now."
    // r bumped 2.1 -> 4 (direct ask: "these should be bigger... tiny...
    // maybe dot x 2?") -- not a blind 2x, since at the old r=2.1/label-at
    // -x+9 spacing a full doubling would land the dot's own right edge
    // PAST the label's left edge (a real, checked collision, not
    // assumed) -- dot center also nudged x+5 -> x+6 and the label pushed
    // x+9 -> x+15 so there's still a clean ~5px gap between them at the
    // new, larger size.
    group.appendChild(svgEl("circle", {
      cx: x - w / 2 + 6, cy: y, r: 4,
      fill: !live.healthy ? COLORS.unhealthy : forecastFailed ? COLORS.forecastWarning : COLORS.healthy,
    }));
    const nameEl = svgEl("text", { x: x - w / 2 + 15, y: y + 2.5, class: "node-label", style: `fill:${COLORS.text}` });
    nameEl.textContent = load.name;
    group.appendChild(nameEl);
    const valueEl = svgEl("text", { x: x + w / 2 - 40, y: y + 3, "text-anchor": "end", class: "node-value-big", style: `fill:${color}` });
    valueEl.textContent = live.text;
    group.appendChild(valueEl);
    const fcEl = svgEl("text", { x: x + w / 2 - 4, y: y + 3, "text-anchor": "end", class: "node-value-sub", style: `fill:${COLORS.textDim}` });
    fcEl.textContent = `fc ${fc.text}`;
    group.appendChild(fcEl);

    return { group, cx: x, cy: y, active, healthy: live.healthy };
  }

  // Directional line: dashed + animated when active, with a large
  // arrowhead at the midpoint. The arrow always points from (x1,y1) toward
  // (x2,y2) -- callers that need to show reversed flow (e.g. the grid
  // connector when exporting) swap their own endpoints, not this function.
  // `colorOverride` lets a specific connector (e.g. grid export) render in
  // a different color than the default orange "flow" color.
  _line(x1, y1, x2, y2, active, colorOverride) {
    const activeColor = colorOverride || COLORS.lineActive;
    const group = svgEl("g", {});
    group.appendChild(svgEl("line", {
      x1, y1, x2, y2,
      stroke: active ? activeColor : COLORS.line,
      "stroke-width": active ? 2.2 : 1.2,
      opacity: active ? 0.95 : 0.35,
      class: active ? "flow-line" : "",
    }));
    if (active) {
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const ang = Math.atan2(y2 - y1, x2 - x1);
      const ah = 13;
      const bx1 = mx - ah * Math.cos(ang - Math.PI / 6.5), by1 = my - ah * Math.sin(ang - Math.PI / 6.5);
      const bx2 = mx - ah * Math.cos(ang + Math.PI / 6.5), by2 = my - ah * Math.sin(ang + Math.PI / 6.5);
      const tipX = mx + ah * 0.55 * Math.cos(ang), tipY = my + ah * 0.55 * Math.sin(ang);
      group.appendChild(svgEl("polygon", {
        points: `${tipX},${tipY} ${bx1},${by1} ${bx2},${by2}`,
        fill: activeColor, stroke: "#161619", "stroke-width": 1,
      }));
    }
    return group;
  }

  // Live proportional supply mix (solar / battery-discharge / grid-import)
  // feeding the switchboard right now -- null when idle. Unit-normalized.
  _busMix(cfg) {
    let solarW = 0;
    (cfg.inverters || []).forEach((inv) => (inv.pv_strings || []).forEach((s) => {
      const w = toWatts(this._entityState(s.entity));
      if (!Number.isNaN(w) && w > 0) solarW += w;
    }));
    const battW = cfg.switchboard.battery_power ? toWatts(this._entityState(cfg.switchboard.battery_power)) : NaN;
    const batteryW = !Number.isNaN(battW) && battW > 0 ? battW : 0;
    const gridW = toWatts(this._entityState(cfg.switchboard.grid_meter));
    const gridImportW = !Number.isNaN(gridW) && gridW > 0 ? gridW : 0;
    const total = solarW + batteryW + gridImportW;
    if (total < 5) return null;
    return { solar: solarW / total, battery: batteryW / total, grid: gridImportW / total };
  }

  _render() {
    if (!this._svg || !this._hass) return;
    const cfg = this._config;
    const lines = [];
    const nodes = [];

    // ---- Inverters: each a tall vertical bar (a smaller, fatter version
    // of the switchboard bus itself) -- PV strings tap its LEFT side via
    // a perfectly straight horizontal line at the string's own height,
    // the battery chain taps its BOTTOM via a single straight vertical
    // drop then chains horizontally. No diagonal lines anywhere here. ----
    const rowGap = 40;
    const rowTop = 50;
    const invBarW = 17;
    const pvW = 72, pvH = 30, pvGap = 9;
    const batW = 100, batH = 52, batGap = 11, batTapDrop = 22;

    const invX = 208;
    let cursorY = rowTop;
    let maxBatteryRightEdge = 0;
    const invResults = [];

    cfg.inverters.forEach((inv) => {
      const strings = inv.pv_strings || [];
      const towers = inv.battery_towers || [];
      const invBarH = Math.max(68, strings.length * (pvH + pvGap) - pvGap + 16);
      const invTop = cursorY;
      const invBottom = invTop + invBarH;
      const invMidY = (invTop + invBottom) / 2;

      // Real per-inverter battery power -- sensor.battery_power_signed_inv{1,2},
      // confirmed live (2026-08-14): signed W, positive = discharging (battery
      // feeding the house through this inverter), negative = charging, same
      // sign convention already used for the whole-system logger_battery_power.
      // No per-tower power/current reading is reliable enough to use (live
      // current read 0.0A on both towers even while one's own parent inverter
      // was actively discharging 13.4kW) -- this per-inverter figure is the
      // most granular real signal available, and drives both the vertical
      // drop and every horizontal tower-connector's direction/activity below,
      // not just a fixed always-on/always-same-direction line.
      const battState = inv.battery_power ? this._entityState(inv.battery_power) : undefined;
      const battVal = fmtValue(battState);
      const battW = toWatts(battState);
      const discharging = !Number.isNaN(battW) && battW > 5;
      const charging = !Number.isNaN(battW) && battW < -5;
      const battActive = discharging || charging;
      // Computed early (rather than inside the PV-strings loop below, which
      // builds the actual string boxes) so the inverter bar's own fill tint
      // -- drawn before that loop runs -- knows whether solar is active too.
      const pvActive = strings.some((str) => {
        const w = toWatts(this._entityState(str.entity));
        return !Number.isNaN(w) && Math.abs(w) > 5;
      });

      // Inverter bar itself.
      const invGroup = svgEl("g", {});
      invGroup.appendChild(svgEl("rect", {
        x: invX - invBarW / 2, y: invTop, width: invBarW, height: invBarH, rx: 8,
        fill: "#161619", stroke: COLORS.inverter, "stroke-width": 2, class: "glow",
      }));
      if (pvActive || battActive) {
        invGroup.appendChild(svgEl("rect", {
          x: invX - invBarW / 2, y: invTop, width: invBarW, height: invBarH, rx: 8,
          fill: pvActive ? COLORS.solar : COLORS.battery, opacity: 0.12,
        }));
      }
      const invLabel = svgEl("text", { x: invX, y: invTop - 20, "text-anchor": "middle", class: "node-label", style: `fill:${COLORS.text}` });
      invLabel.textContent = inv.name;
      invGroup.appendChild(invLabel);

      // Real per-inverter total DC power (sensor.total_dc_power_inv{1,2}) --
      // genuinely different from battery_power above: DC power is total PV
      // throughput regardless of whether the battery is doing anything at
      // all. Direct household catch, 2026-08-22: this header used to show
      // ONLY battery_power, so a fully-charged inverter with real solar
      // flowing straight through to the switchboard (battery legitimately
      // 0W, both its towers full) looked falsely idle. Kept as ONE combined
      // line (not a new third text row) deliberately -- this card has real,
      // documented history of layout-overlap bugs from stacking more text
      // than a header's own reserved vertical space accounts for; a single
      // line needs zero new headroom. Battery flow is appended only when
      // genuinely active (not shown as "▲ 0 W" clutter when idle, which
      // is most of the time) -- it's still fully represented separately by
      // the tower-connector arrows below regardless of whether it's named
      // here.
      const dcState = inv.dc_power ? this._entityState(inv.dc_power) : undefined;
      const dcVal = fmtValue(dcState);
      const dcW = toWatts(dcState);
      const dcHealthy = dcVal.healthy;
      if (dcState || battState) {
        const parts = [];
        if (dcHealthy) parts.push(dcVal.text);
        if (battActive && battVal.healthy) parts.push(`${discharging ? "▼" : "▲"} ${battVal.text}`);
        const subLabel = svgEl("text", {
          x: invX, y: invTop - 8, "text-anchor": "middle", class: "node-value-sub",
          style: `fill:${dcHealthy && dcW > 5 ? COLORS.solar : (battActive ? COLORS.battery : COLORS.textDim)}`,
        });
        subLabel.textContent = parts.length ? parts.join(" · ") : "—";
        invGroup.appendChild(subLabel);
      }
      nodes.push(invGroup);

      // PV strings: one per tap point, evenly spaced along the bar's
      // height, connected by a straight horizontal line at that exact Y.
      strings.forEach((str, si) => {
        const tapY = invTop + ((si + 0.5) / strings.length) * invBarH;
        const px = invX - invBarW / 2 - 40 - pvW / 2;
        const state = this._entityState(str.entity);
        const val = fmtValue(state);
        const w = toWatts(state);
        const isActive = val.healthy && Math.abs(w) > 5;
        // Same fix already applied to loads: a healthy-but-idle string (the
        // entire card, every night) was rendering in full solar-orange
        // regardless of actually producing anything.
        const node = this._box(px, tapY, pvW, pvH, {
          iconKind: "sun", label: `MPPT${str.mppt}`, valueText: val.text,
          color: isActive ? COLORS.solar : COLORS.textDim, healthy: val.healthy, active: isActive,
        });
        nodes.push(node.group);
        lines.push(this._line(px + pvW / 2, tapY, invX - invBarW / 2, tapY, isActive));
      });

      // Battery chain: one straight vertical drop from the bar's own
      // bottom-center, then chained horizontally between towers. Direction
      // now follows real flow: discharging reverses both the drop and every
      // tower connector so the arrow points battery -> inverter (matching
      // "outputting"); charging keeps the original inverter -> battery
      // direction; idle draws inactive (no arrow at all), instead of the
      // old always-on, always-one-direction line.
      if (towers.length) {
        const batY = invBottom + batTapDrop + batH / 2;
        if (discharging) {
          lines.push(this._line(invX, batY, invX, invBottom, true, COLORS.battery));
        } else {
          lines.push(this._line(invX, invBottom, invX, batY, charging, COLORS.battery));
        }
        let rowRightEdge = invX;
        towers.forEach((tower, ti) => {
          const bx = invX + batW / 2 + 20 + ti * (batW + batGap);
          const batNode = this._batteryBox(bx, batY, batW, batH, tower, battActive);
          nodes.push(batNode.group);
          const segActive = battActive && batNode.healthy;
          if (discharging) {
            lines.push(this._line(bx - batW / 2, batY, rowRightEdge, batY, segActive, COLORS.battery));
          } else {
            lines.push(this._line(rowRightEdge, batY, bx - batW / 2, batY, segActive, COLORS.battery));
          }
          rowRightEdge = bx + batW / 2;
        });
        maxBatteryRightEdge = Math.max(maxBatteryRightEdge, rowRightEdge);
      }

      const rowBottom = towers.length ? invBottom + batTapDrop + batH + 10 : invBottom + 10;
      // Solar takes color priority when both are present (rare -- daytime
      // dispatch), since PV is the more informative signal to highlight;
      // otherwise this is purely battery-driven, matching what's actually
      // happening right now (no sun, house running on battery).
      invResults.push({
        x: invX + invBarW / 2, y: invMidY,
        active: pvActive || battActive,
        color: pvActive ? undefined : battActive ? COLORS.battery : undefined,
      });
      cursorY = rowBottom + rowGap;
    });

    const invZoneBottom = cursorY - rowGap;

    // ---- Loads: single column, each with its OWN direct line straight
    // off the main switchboard bus -- the bus itself is the only "spine",
    // extended to cover the full load-list height, since every load is
    // literally directly wired to the switchboard in reality. ----
    const loads = this._discoverLoads();
    const tileW = 300, tileH = 20, tileGap = 4;
    // busX must clear the widest battery row too, not just a fixed margin
    // off the inverter bar -- this exact bug (bus rendering on top of the
    // second tower) already happened once this session from forgetting
    // this, confirmed again live via the local render just now.
    const busX = Math.max(invX + invBarW / 2 + 680, maxBatteryRightEdge + 50);
    const loadX0 = busX + 260;
    const loadsTop = rowTop;
    const loadsBottom = loadsTop + loads.length * (tileH + tileGap) - tileGap;

    // ---- Switchboard bus: spans whichever is taller, the inverter zone
    // or the loads column, so every element on either side can tap it
    // directly with a single straight line. ----
    const busTop = rowTop - 15;
    const busBottom = Math.max(invZoneBottom, loadsBottom) + 15;
    const busH = busBottom - busTop;
    const busMidY = (busTop + busBottom) / 2;

    const mix = this._busMix(cfg);
    // Dominant-source color (2026-08-15, explicit ask: "anything from
    // the grid should be RED incl the switchboard") -- computed here,
    // before the bus outline is drawn, so the SAME value already used
    // for the load connectors below also drives the switchboard's own
    // border. Falls back to the fixed neutral COLORS.bus only when mix
    // itself is unavailable (e.g. a real entity reporting unavailable).
    let dominantColor = COLORS.bus;
    if (mix) {
      const bySize = [["solar", COLORS.solar], ["battery", COLORS.battery], ["grid", COLORS.gridImport]]
        .sort((a, b) => mix[b[0]] - mix[a[0]]);
      dominantColor = bySize[0][1];
    }
    const busGroup = svgEl("g", {});
    busGroup.appendChild(svgEl("rect", {
      x: busX - invBarW / 2, y: busTop, width: invBarW, height: busH, rx: 8,
      fill: "#0c0c0e", stroke: dominantColor, "stroke-width": 2.2, class: "glow",
    }));
    if (mix) {
      const segs = [["solar", COLORS.solar], ["battery", COLORS.battery], ["grid", COLORS.gridNode]];
      let segY = busTop + 3;
      const innerH = busH - 6;
      segs.forEach(([key, color]) => {
        const segH = mix[key] * innerH;
        if (segH > 0.5) {
          busGroup.appendChild(svgEl("rect", {
            x: busX - invBarW / 2 + 3, y: segY, width: invBarW - 6, height: segH,
            fill: color, opacity: 0.85,
          }));
        }
        segY += segH;
      });
    }
    const busLabel = svgEl("text", { x: busX, y: busTop - 24, "text-anchor": "middle", class: "node-label", style: `fill:${COLORS.text}` });
    busLabel.textContent = "Switchboard";
    busGroup.appendChild(busLabel);
    nodes.push(busGroup);

    invResults.forEach((r) => {
      lines.push(this._line(r.x, r.y, busX - invBarW / 2, r.y, r.active, r.color));
    });

    // Real, live list of which (if any) load forecasts failed during the
    // Solver's last solve (2026-08-17, see COLORS.forecastWarning's own
    // comment) -- read once per render from the same standalone
    // aggregate sensor nimbus_solver_forecast_writer.py already
    // publishes (sensor.nimbus_household_load_total_forecast), not
    // re-derived or separately polled here. Missing/unavailable sensor
    // (e.g. before the writer's first ever run) degrades to an empty
    // Set, same as "nothing has failed" -- never breaks rendering.
    const failedForecastsState = this._entityState("sensor.nimbus_household_load_total_forecast");
    const failedForecasts = new Set(
      (failedForecastsState && failedForecastsState.attributes && failedForecastsState.attributes.failed_load_entities) || []
    );

    // Load connector lines match whichever source (solar/battery/grid-
    // import) is genuinely dominant right now, same color language as
    // everywhere else on the card: orange=solar, blue=battery, red=grid
    // import ("grid is evil" -- deliberate design choice, not a health/
    // warning indicator here). dominantColor itself is computed above
    // (before the switchboard bus outline is drawn), reused here as-is.
    loads.forEach((load, i) => {
      const ly = loadsTop + i * (tileH + tileGap);
      const tile = this._loadTile(loadX0 + tileW / 2, ly, tileW, tileH, load, failedForecasts);
      lines.push(this._line(busX + invBarW / 2, ly, loadX0, ly, tile.active, dominantColor));
      nodes.push(tile.group);
    });

    // ---- Grid: centered on the bus, with live price/FIT and today's
    // import/export kWh. ----
    const gmW = 150, gmH = 68;
    const gmY = busTop - 70;
    const meterState = this._entityState(cfg.switchboard.grid_meter);
    const meterVal = fmtValue(meterState);
    const buyPrice = cfg.switchboard.import_price ? numOf(this._entityState(cfg.switchboard.import_price)) : NaN;
    const sellPrice = cfg.switchboard.export_price ? numOf(this._entityState(cfg.switchboard.export_price)) : NaN;
    const impDaily = cfg.switchboard.import_energy_daily ? fmtValue(this._entityState(cfg.switchboard.import_energy_daily)) : null;
    const expDaily = cfg.switchboard.export_energy_daily ? fmtValue(this._entityState(cfg.switchboard.export_energy_daily)) : null;
    const gmSub = [];
    if (!Number.isNaN(buyPrice) || !Number.isNaN(sellPrice)) {
      gmSub.push(`Buy ${Number.isNaN(buyPrice) ? "—" : "$" + buyPrice.toFixed(2)}  ·  Sell ${Number.isNaN(sellPrice) ? "—" : "$" + sellPrice.toFixed(2)}`);
    }
    if (impDaily || expDaily) {
      gmSub.push(`↓${impDaily ? impDaily.text : "—"}  ↑${expDaily ? expDaily.text : "—"}`);
    }
    // logger_meter_total_active_power sign convention, confirmed directly
    // against a live reading (-11.5kW while genuinely selling): negative =
    // exporting/selling, positive = importing/buying -- matches how
    // _busMix() already treats this same entity elsewhere in this file.
    const meterW = toWatts(meterState);
    const exporting = meterVal.healthy && !Number.isNaN(meterW) && meterW < -5;
    const importing = meterVal.healthy && !Number.isNaN(meterW) && meterW > 5;
    const gridActive = exporting || importing;
    const gmNode = this._box(busX, gmY, gmW, gmH, {
      iconKind: "bolt", label: "Grid", valueText: meterVal.text, subLines: gmSub,
      color: exporting ? COLORS.gridSell : COLORS.gridNode, healthy: meterVal.healthy,
      active: gridActive,
    });
    nodes.push(gmNode.group);
    // Importing: arrow points from Grid INTO the switchboard (original
    // direction). Exporting: arrow points OUT of the switchboard toward
    // Grid instead -- swap the line's own endpoints to reverse it, and
    // color it to match the box (blue, not the default orange).
    if (exporting) {
      lines.push(this._line(busX, busTop, gmNode.cx, gmY + gmH / 2, gridActive, COLORS.gridSell));
    } else {
      lines.push(this._line(gmNode.cx, gmY + gmH / 2, busX, busTop, gridActive));
    }

    // ---- Whole-house headline + real daily kWh stats (solar generation,
    // battery charge/discharge, house load) sourced from this system's
    // own already-working sunsynk/power-flow-card-plus configs. Each daily
    // figure gets its own bold number + small caption block (matching the
    // reference card's own style) instead of being buried in one small
    // dim line -- these were reported as "still missing" when they were
    // too small/crammed to notice. ----
    const wh = cfg.whole_house;
    let whGroup = null;
    let whHeight = 0;
    if (wh) {
      const whLive = fmtValue(this._entityState(wh.live));
      const whFc = fmtValue(this._entityState(wh.forecast));
      const houseDaily = cfg.switchboard.house_load_energy_daily ? fmtValue(this._entityState(cfg.switchboard.house_load_energy_daily)) : null;
      const solarDaily = cfg.switchboard.solar_energy_daily ? fmtValue(this._entityState(cfg.switchboard.solar_energy_daily)) : null;
      const chgDaily = cfg.switchboard.battery_charge_daily ? fmtValue(this._entityState(cfg.switchboard.battery_charge_daily)) : null;
      const dischgDaily = cfg.switchboard.battery_discharge_daily ? fmtValue(this._entityState(cfg.switchboard.battery_discharge_daily)) : null;

      whHeight = 78;
      whGroup = svgEl("g", {});
      whGroup.appendChild(icon("house", 22, 22, 9, COLORS.wh));
      const val = svgEl("text", { x: 38, y: 24, class: "wh-value", style: `fill:${COLORS.wh}` });
      val.textContent = `${whLive.text}  /  ${whFc.text}`;
      whGroup.appendChild(val);
      const lbl = svgEl("text", { x: 38, y: 36, class: "wh-label", style: `fill:${COLORS.textDim}` });
      lbl.textContent = "WHOLE HOUSE — LIVE / FORECAST";
      whGroup.appendChild(lbl);

      const statBlocks = [];
      if (solarDaily) statBlocks.push({ value: solarDaily.text, label: "DAILY SOLAR", color: COLORS.solar });
      if (chgDaily || dischgDaily) statBlocks.push({ value: `↓${chgDaily ? chgDaily.text : "—"} ↑${dischgDaily ? dischgDaily.text : "—"}`, label: "DAILY BATTERY", color: COLORS.battery });
      if (houseDaily) statBlocks.push({ value: houseDaily.text, label: "DAILY LOAD", color: COLORS.wh });
      statBlocks.forEach((s, i) => {
        const sx = 20 + i * 155;
        const sv = svgEl("text", { x: sx, y: 60, class: "stat-value", style: `fill:${s.color}` });
        sv.textContent = s.value;
        whGroup.appendChild(sv);
        const sl = svgEl("text", { x: sx, y: 71, class: "stat-label", style: `fill:${COLORS.textDim}` });
        sl.textContent = s.label;
        whGroup.appendChild(sl);
      });

      // Basic weather: icon + temperature + condition + a live clock.
      // Positioned to the right of the daily stat blocks -- plenty of
      // empty header-row space out there before the Grid box's own
      // column starts.
      const wxState = cfg.weather ? this._entityState(cfg.weather) : undefined;
      if (wxState) {
        const wxHealthy = wxState.state !== "unavailable" && wxState.state !== "unknown";
        const wxKind = WEATHER_ICON_KIND[wxState.state] || "cloud";
        const wxTemp = wxState.attributes && wxState.attributes.temperature;
        const wxUnit = (wxState.attributes && wxState.attributes.temperature_unit) || "°C";
        const wxX = 20 + statBlocks.length * 155 + 15;
        whGroup.appendChild(icon(wxKind, wxX, 55, 15, wxHealthy ? COLORS.wh : COLORS.unavailable));
        const wxVal = svgEl("text", { x: wxX + 26, y: 60, class: "stat-value", style: `fill:${wxHealthy ? COLORS.wh : COLORS.unavailable}` });
        wxVal.textContent = wxHealthy && typeof wxTemp === "number" ? `${wxTemp.toFixed(0)}${wxUnit}` : "—";
        whGroup.appendChild(wxVal);
        const now = new Date();
        const hh = String(now.getHours()).padStart(2, "0");
        const mm = String(now.getMinutes()).padStart(2, "0");
        const conditionLabel = wxHealthy ? wxState.state.replace(/-/g, " ") : "unavailable";
        const wxLbl = svgEl("text", { x: wxX + 26, y: 71, class: "stat-label", style: `fill:${COLORS.textDim}` });
        wxLbl.textContent = `${conditionLabel} · ${hh}:${mm}`;
        whGroup.appendChild(wxLbl);
      }
    }

    const totalW = Math.max(loadX0 + tileW, busX + 260) + 20;
    const totalH = busBottom + 20;
    // Prefer the real, live version (read off any Nimbus device's own
    // sw_version, see _discoverNimbusVersion() above); falls back to the
    // static topology_map.yaml value only if the live lookup can't find
    // anything (older HA frontend, or a Nimbus version predating this).
    const nimbusVersion = this._discoverNimbusVersion() || cfg.nimbus_version;
    const footerHeight = nimbusVersion ? 20 : 0;

    this._svg.setAttribute("viewBox", `0 0 ${totalW} ${totalH + whHeight + footerHeight}`);
    // Mobile fix (see the CSS comment in _buildStaticShell()): floors the
    // rendered width at this diagram's own true native pixel scale, every
    // render (totalW itself varies with the real, auto-discovered load
    // count) -- a container wider than this still gets the existing 100%
    // stretch-to-fill behavior untouched; a container narrower than this
    // (a phone) now scrolls instead of squeezing every label illegible.
    this._svg.style.minWidth = `${totalW}px`;
    this._svg.innerHTML = "";
    const defs = svgEl("defs", {}, [
      svgEl("filter", { id: "glow", x: "-60%", y: "-60%", width: "220%", height: "220%" }, [
        svgEl("feGaussianBlur", { stdDeviation: 2.2, result: "blur" }),
        svgEl("feMerge", {}, [
          svgEl("feMergeNode", { in: "blur" }),
          svgEl("feMergeNode", { in: "SourceGraphic" }),
        ]),
      ]),
    ]);
    this._svg.appendChild(defs);

    const shiftGroup = svgEl("g", { transform: `translate(0, ${whHeight})` });
    lines.forEach((l) => shiftGroup.appendChild(l));
    nodes.forEach((n) => shiftGroup.appendChild(n));
    this._svg.appendChild(shiftGroup);
    if (whGroup) this._svg.appendChild(whGroup);

    if (nimbusVersion) {
      const footer = svgEl("text", {
        x: totalW / 2, y: whHeight + totalH + 14, "text-anchor": "middle",
        class: "node-value-sub", style: `fill:${COLORS.textDim}`,
      });
      footer.textContent = `Powered by Nimbus v${nimbusVersion}`;
      this._svg.appendChild(footer);
    }
  }
}

customElements.define("switchboard-topology-card", TopologyCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "switchboard-topology-card",
  name: "Topology Card",
  description: "Real switchboard power-flow diagram — inverters, batteries, loads.",
});
