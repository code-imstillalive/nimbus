// nimbus-flow-card.js
//
// The Power Flow tab (household, 3 Nov 2026): what is happening right now,
// what the plan says for right now, and whether it is being followed, as a
// live animated flow, with the Control Panel's own forecast table beside it
// and today's totals underneath.
//
// No settings of its own: everything comes from what Nimbus is set up with,
// so the picture draws itself once the site is set up for the Topology card.
//   * sensor.nimbus_topology_config: power sources (inverters), PV strings,
//     battery towers, controllable loads and the switchboard's daily sensors.
//   * sensor.nimbus_solver_config: the solar, battery and SoC sensors the
//     Solver uses, and its battery sign convention.
//   * sensor.nimbus_solver_battery_forecast: the plan's current period (its
//     flows, prices and SoC) and the next change.
//   * Nimbus's power-signal forecasts: each one's source_sensor is a circuit
//     shown as a small bubble under House while it draws.
//   * switch.nimbus_solver_price_consolidation_live: blanks Cons in the table
//     while consolidation is off (the table's own rule).
// The forecast table is nimbus-dispatch-card-v4 itself in table_only mode, so
// it is the Control Panel's table by construction.
// Anything not set up is left out: a site with no inverters set up gets one
// combined "Solar & battery" node, and one with no towers draws none.
// An EV (a battery participant) is not an inverter: it is drawn once, off
// House, with its SoC in its ring.

const FLOW_COLORS = {
  solar: "#f5b53d",
  battery: "#3ddc84",
  // An EV is a battery of its own kind: a darker green (household, 6 Nov 2026).
  ev: "#1f9d62",
  grid: "#4fa3ff",
  house: "#e8eaf0",
  amber: "#ffb340",
  ghost: "#6b7280",
  p2p: "#c792ea",
  muted: "#9aa0ac",
  line: "#2c323a",
  card: "#1c2026",
};
const FLOW_MIN_KW = 0.05; // below this a line is not drawn
// A circuit bubble shows above 5 W, the Topology card's own "active" mark
// (household, 5 Nov 2026: whatever is powered should show; a tester's 23 W
// lighting circuit was hidden at the old 50 W).
const FLOW_CIRCUIT_MIN_KW = 0.005;
const FLOW_OFF_PLAN_SECONDS = 120; // off plan only after this long
const FLOW_SVG_NS = "http://www.w3.org/2000/svg";

function flowEscape(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function flowKwText(kw) {
  const a = Math.abs(kw);
  return a >= 1 ? a.toFixed(1) + " kW" : Math.round(a * 1000) + " W";
}

class NimbusFlowCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    this._offSince = null;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this._buildShell();
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (this._table) this._table.hass = hass;
    const now = Date.now();
    if (this._lastRender && now - this._lastRender < 2000) {
      if (!this._pending) this._pending = setTimeout(() => { this._pending = null; this._render(); }, 2000);
      return;
    }
    this._render();
  }

  getCardSize() {
    return 14;
  }

  getGridOptions() {
    return { columns: "full", rows: "auto" };
  }

  // --- reading Nimbus -------------------------------------------------------

  _state(id) {
    return id && this._hass ? this._hass.states[id] : undefined;
  }

  _attrs(id) {
    const s = this._state(id);
    return (s && s.attributes) || {};
  }

  _kw(id) {
    const s = this._state(id);
    if (!s) return NaN;
    const v = parseFloat(s.state);
    if (isNaN(v)) return NaN;
    const unit = String((s.attributes || {}).unit_of_measurement || "").toLowerCase();
    if (unit === "w") return v / 1000;
    if (unit === "mw") return v * 1000;
    return v;
  }

  _num(id) {
    const s = this._state(id);
    const v = s ? parseFloat(s.state) : NaN;
    return v;
  }

  _site() {
    const topo = this._attrs("sensor.nimbus_topology_config");
    const cfg = this._attrs("sensor.nimbus_solver_config");
    const battSign = cfg.solver_battery_power_positive_is_charge ? -1 : 1; // + = discharging
    const switchboard = topo.switchboard || {};
    const meters = topo.forecaster_meters || {};
    const gridSensor = switchboard.switchboard_grid_meter_sensor || meters.grid_meter || cfg.grid_sensor || "";
    const battSensor = cfg.solver_battery_power_sensor || switchboard.switchboard_battery_power_sensor || meters.battery_power || "";
    const solarSensor = cfg.solver_solar_power_sensor || "";
    const socSensor = cfg.solver_battery_soc_sensor || "";
    const loadForecast = cfg.solver_load_forecast_sensor || "";
    const houseSensor = this._attrs(loadForecast).source_sensor || "";

    // A battery participant (an EV) comes through as a derived power source
    // with a tower of its own; every other source is an inverter.
    const isEv = (ps) => !!ps.participant || (!!ps.derived && ps.subentry_id !== "derived_home" && !ps.power_source_dc_sensor);
    const allSources = topo.power_sources || [];
    const evIds = new Set(allSources.filter(isEv).map((ps) => ps.subentry_id));
    const sources = allSources.filter((ps) => !isEv(ps)).map((ps) => ({
      id: ps.subentry_id,
      name: ps.power_source_name || "Inverter",
      dc: ps.power_source_dc_sensor || "",
      batt: ps.power_source_battery_sensor || "",
    }));
    const strings = (topo.pv_strings || []).map((s) => ({
      entity: s.pv_string_entity,
      label: s.pv_string_label || s.pv_string_entity,
      source: s.pv_string_power_source,
    }));
    const towers = (topo.battery_towers || []).filter((t) => !evIds.has(t.battery_tower_power_source)).map((t) => ({
      title: t.title || "Tower",
      soc: t.battery_tower_soc_sensor,
      soh: t.battery_tower_soh_sensor,
      temp: t.battery_tower_temperature_sensor,
      source: t.battery_tower_power_source,
    }));
    const evs = allSources.filter(isEv).map((ps) => {
      const tower = (topo.battery_towers || []).find((t) => t.battery_tower_power_source === ps.subentry_id) || {};
      return {
        name: tower.title || ps.power_source_name || "EV",
        power: ps.power_source_battery_sensor || "",
        soc: tower.battery_tower_soc_sensor || "",
        chargeSign: ps.power_positive_is_charge ? 1 : -1, // reading x sign = + charging
      };
    });
    const loads = (topo.controllable_loads || []).filter((l) => l && l.power_sensor);
    return { cfg, battSign, switchboard, gridSensor, battSensor, solarSensor, socSensor, houseSensor, sources, strings, towers, evs, loads };
  }

  // The circuits inside House: every Nimbus power signal whose source is a
  // power sensor other than the ones already drawn.
  _circuits(site, drawn) {
    // The household's own loads first, under the names they gave them; the
    // forecast-sensor scan then adds only circuits not already listed (5 Nov
    // 2026: it ran first and showed raw sensor names over the household's).
    const out = [];
    for (const l of site.loads) {
      if (drawn.has(l.power_sensor)) continue;
      const kw = this._kw(l.power_sensor);
      if (kw > FLOW_CIRCUIT_MIN_KW && !out.some((c) => c.src === l.power_sensor)) out.push({ name: l.name || l.power_sensor, kw, src: l.power_sensor });
    }
    for (const [id, st] of Object.entries(this._hass.states)) {
      if (!id.startsWith("sensor.nimbus_") || !id.endsWith("_forecast")) continue;
      const a = st.attributes || {};
      const src = a.source_sensor;
      if (!src || src.startsWith("sensor.nimbus_") || drawn.has(src)) continue;
      if (out.some((c) => c.src === src)) continue;
      if (a.signal_role && a.signal_role !== "other") continue;
      const unit = String((this._attrs(src).unit_of_measurement) || "").toLowerCase();
      if (unit !== "w" && unit !== "kw") continue;
      const kw = this._kw(src);
      if (!(kw > FLOW_CIRCUIT_MIN_KW)) continue;
      // Named after the household's own signal (the forecast entity carries
      // its title), not the raw sensor (5 Nov 2026: "Smappee MQTT Circuit 1
      // Realtime W" for a circuit its owner called "Circuit - Power 1").
      const name = String(a.friendly_name || this._attrs(src).friendly_name || src)
        .replace(/\s*forecast$/i, "")
        // A trailing "power" / "active import" only, so "Circuit - Power 1"
        // keeps its word.
        .replace(/\s*\b(active import)\s*$/i, "")
        .replace(/\s*\bpower\s*$/i, "")
        .replace(/^CB-(PW|LT)\s*/i, (m, g) => (g.toUpperCase() === "LT" ? "Lights " : ""))
        .trim();
      out.push({ name: name || src, kw, src });
    }
    out.sort((x, y) => y.kw - x.kw);
    return out.slice(0, 12);
  }

  _plan() {
    const fc = this._attrs("sensor.nimbus_solver_battery_forecast").forecast;
    return Array.isArray(fc) && fc.length ? fc : null;
  }

  // --- the model: nodes and flows for this moment ---------------------------

  _model() {
    const site = this._site();
    const plan = this._plan();
    const p0 = plan ? plan[0] : null;
    const drawn = new Set([site.gridSensor, site.battSensor, site.solarSensor, site.houseSensor]);

    // Inverters (power sources); without any, one combined node.
    let invs = site.sources.map((s) => {
      drawn.add(s.dc);
      drawn.add(s.batt);
      return { ...s, solar: Math.max(0, this._kw(s.dc) || 0), battRaw: this._kw(s.batt) };
    });
    const totalBatt = (this._kw(site.battSensor) || 0) * site.battSign;
    if (!invs.length) {
      invs = [{ id: "_all", name: "Solar & battery", solar: Math.max(0, this._kw(site.solarSensor) || 0), battRaw: totalBatt, combined: true }];
    }
    // Per-inverter battery: + = discharging. Their own sensors' sign is
    // checked against the Solver's battery sensor; failing that, the total
    // is shared by tower count.
    const raws = invs.map((i) => i.battRaw).filter((v) => !isNaN(v));
    let sign = 1;
    const rawSum = raws.reduce((a, b) => a + b, 0);
    if (Math.abs(totalBatt) > 0.3 && raws.length && rawSum * totalBatt < 0) sign = -1;
    for (const i of invs) i.batt = isNaN(i.battRaw) ? 0 : i.battRaw * (i.combined ? 1 : sign);
    if (!raws.length && invs.length) for (const i of invs) i.batt = totalBatt / invs.length;
    for (const s of site.strings) drawn.add(s.entity);

    const solarTotal = invs.reduce((a, i) => a + i.solar, 0);
    const gridKw = this._kw(site.gridSensor); // + = importing
    let house = this._kw(site.houseSensor);
    const acOut = invs.map((i) => i.solar + i.batt); // + = to the AC side
    if (isNaN(house)) house = Math.max(0, acOut.reduce((a, b) => a + b, 0) + (isNaN(gridKw) ? 0 : gridKw));
    const grid = isNaN(gridKw) ? 0 : gridKw;

    // Share every supply on the AC side across every demand, in proportion.
    const supplies = [];
    const demands = [{ id: "house", kw: Math.max(0, house) }];
    invs.forEach((i, k) => {
      const v = acOut[k];
      const colour = i.batt > 0.05 && i.batt >= i.solar ? "battery" : "solar";
      if (v > FLOW_MIN_KW) supplies.push({ id: "inv" + k, kw: v, colour });
      else if (v < -FLOW_MIN_KW) demands.push({ id: "inv" + k, kw: -v });
    });
    if (grid > FLOW_MIN_KW) supplies.push({ id: "grid", kw: grid, colour: "grid" });
    else if (grid < -FLOW_MIN_KW) demands.push({ id: "grid", kw: -grid });
    const supply = supplies.reduce((a, s) => a + s.kw, 0);
    const flows = [];
    if (supply > 0) {
      for (const s of supplies) {
        for (const d of demands) {
          if (s.id === d.id) continue;
          const kw = (s.kw * d.kw) / supply;
          if (kw > FLOW_MIN_KW) flows.push({ from: s.id, to: d.id, kw, colour: s.colour });
        }
      }
    }
    // Strings into their inverter, towers to and from theirs.
    site.strings.forEach((s, k) => {
      const kw = Math.max(0, this._kw(s.entity) || 0);
      const inv = invs.findIndex((i) => i.id === s.source);
      if (kw > FLOW_MIN_KW && inv >= 0) flows.push({ from: "str" + k, to: "inv" + inv, kw, colour: "solar" });
    });
    const towersOf = (k) => site.towers.map((t, j) => ({ t, j })).filter(({ t }) => t.source === invs[k].id);
    invs.forEach((i, k) => {
      const ts = towersOf(k);
      if (!ts.length || Math.abs(i.batt) <= FLOW_MIN_KW) return;
      const each = i.batt / ts.length;
      for (const { j } of ts) {
        if (each > 0) flows.push({ from: "tow" + j, to: "inv" + k, kw: each, colour: "battery" });
        else flows.push({ from: "inv" + k, to: "tow" + j, kw: -each, colour: grid > FLOW_MIN_KW ? "grid" : "solar" });
      }
    });

    // EVs off House: charging draws from it, discharging feeds it.
    const evs = site.evs.map((e, k) => {
      drawn.add(e.power);
      const raw = this._kw(e.power);
      const charge = isNaN(raw) ? 0 : raw * e.chargeSign; // + = charging
      if (charge > FLOW_MIN_KW) flows.push({ from: "house", to: "ev" + k, kw: charge, colour: grid > FLOW_MIN_KW ? "grid" : "solar" });
      else if (charge < -FLOW_MIN_KW) flows.push({ from: "ev" + k, to: "house", kw: -charge, colour: "ev" });
      return { ...e, charge, socPct: this._num(e.soc) };
    });

    // Plan check: the battery against the plan's current period, held for
    // FLOW_OFF_PLAN_SECONDS before it counts.
    let offPlan = false;
    const ghosts = [];
    if (p0) {
      const planBatt = parseFloat(p0.battery_kw) || 0;
      const tol = Math.max(1.5, Math.abs(planBatt) * 0.25);
      const differs = Math.abs(planBatt - totalBatt) > tol;
      const now = Date.now();
      if (differs) {
        if (this._offSince === null) this._offSince = now;
        offPlan = now - this._offSince > FLOW_OFF_PLAN_SECONDS * 1000;
      } else this._offSince = null;
      if (offPlan) {
        const planExport = parseFloat(p0.grid_export_kw) || 0;
        const planImport = parseFloat(p0.grid_import_kw) || 0;
        invs.forEach((_, k) => {
          if (planExport > 1 && grid > -planExport * 0.3) ghosts.push({ from: "inv" + k, to: "grid" });
          if (planImport > 1 && grid < planImport * 0.3) ghosts.push({ from: "grid", to: "inv" + k });
        });
      }
    }

    return { site, plan, p0, invs, evs, house, grid, solarTotal, totalBatt, flows, ghosts, offPlan, circuits: this._circuits(site, drawn) };
  }

  // --- drawing ----------------------------------------------------------------

  _layout(m) {
    const n = m.invs.length;
    // Household layout, 6 Nov 2026: Grid at the top between the inverters,
    // House in the centre, EVs beside House, so the whole lower half is a
    // fan of circuits (up to a half circle, with room for sub-devices).
    const W = 860, pos = {};
    const invY = 230, houseY = 440;
    m.invs.forEach((_, k) => {
      pos["inv" + k] = { x: n === 1 ? W / 2 - 160 : 260 + ((W - 520) * k) / Math.max(1, n - 1), y: invY, r: 46 };
    });
    pos.house = { x: W / 2, y: houseY, r: 50 };
    pos.grid = { x: n === 1 ? W / 2 + 160 : W / 2, y: n === 1 ? invY : 120, r: 46 };
    // Strings above their inverter.
    m.invs.forEach((inv, k) => {
      const mine = m.site.strings.map((s, j) => ({ s, j })).filter(({ s }) => s.source === inv.id);
      mine.forEach(({ j }, q) => {
        const p = pos["inv" + k];
        pos["str" + j] = { x: p.x + (q - (mine.length - 1) / 2) * 120, y: 62, r: 26 };
      });
    });
    // Towers beside their inverter: the left half's in a column on the left,
    // the rest in a column on the right, each column spaced to fit.
    const cols = { left: [], right: [] };
    m.invs.forEach((inv, k) => {
      const side = n > 1 && k >= n / 2 ? "right" : "left";
      m.site.towers.forEach((t, j) => { if (t.source === inv.id) cols[side].push(j); });
    });
    for (const [side, js] of Object.entries(cols)) {
      const c = js.length, step = Math.min(140, 380 / Math.max(1, c)), r = step < 110 ? 26 : 32;
      js.forEach((j, q) => {
        pos["tow" + j] = { x: side === "left" ? 70 : W - 70, y: invY + (q - (c - 1) / 2) * step, r };
      });
    }
    // EVs in a row above House's level, inside the tower columns: the first
    // two on the right, a third and fourth mirrored on the left, so none
    // meets the circuit fan, whose edges reach House's level.
    const evX = [W / 2 + 170, W / 2 + 250, W / 2 - 170, W / 2 - 250];
    m.evs.forEach((_, k) => {
      pos["ev" + k] = { x: evX[k % 4], y: houseY - 100 - Math.floor(k / 4) * 80, r: 30 };
    });
    // Circuits: a fan below House that widens with their number, up to a
    // half circle for 12.
    const cs = m.circuits, c = cs.length;
    const span = c <= 1 ? 0 : Math.min(Math.PI, (Math.PI * (c - 1)) / 11 + 0.5);
    const R = 260;
    cs.forEach((_, i) => {
      const a = c <= 1 ? Math.PI / 2 : Math.PI / 2 - span / 2 + (span * i) / (c - 1);
      pos["cir" + i] = { x: W / 2 - R * Math.cos(a), y: houseY + R * Math.sin(a) };
    });
    return { W, H: houseY + R + 60, pos };
  }

  _buildShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        /* 5 Nov 2026: fall back through the theme's own card colour, as the text
           does, so a theme without --ha-card-background never gets dark text on
           this card's dark fallback. */
        ha-card { padding: 14px 16px 16px; background: var(--ha-card-background, var(--card-background-color, ${FLOW_COLORS.card})); }
        .grid { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 16px; align-items: stretch; }
        @media (max-width: 1040px) { .grid { grid-template-columns: 1fr; } }
        .flowcol { min-width: 0; display: flex; flex-direction: column; }
        .head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
        .head h2 { margin: 0; font-size: 15px; font-weight: 600; }
        .head h2 span { color: var(--secondary-text-color, ${FLOW_COLORS.muted}); font-weight: 400; }
        .pills { display: flex; flex-wrap: wrap; gap: 6px; }
        .pill { font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px; display: inline-flex; align-items: center; gap: 6px; }
        .pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
        .pill.ok { color: ${FLOW_COLORS.battery}; background: rgba(61,220,132,0.12); }
        .pill.off { color: ${FLOW_COLORS.amber}; background: rgba(255,179,64,0.14); }
        .pill.sun { color: ${FLOW_COLORS.solar}; background: rgba(245,181,61,0.12); }
        .pill.sun.low { color: ${FLOW_COLORS.amber}; background: rgba(255,179,64,0.14); }
        svg { display: block; width: 100%; height: auto; }
        .flow { fill: none; stroke-linecap: round; }
        .run { animation: run linear infinite; }
        @keyframes run { to { stroke-dashoffset: -32; } }
        @media (prefers-reduced-motion: reduce) { .run { animation: none; } }
        .ghost { fill: none; stroke: ${FLOW_COLORS.ghost}; stroke-width: 2; stroke-dasharray: 3 7; }
        .ring { fill: var(--ha-card-background, var(--card-background-color, ${FLOW_COLORS.card})); stroke-width: 3; }
        .ring.house { stroke: var(--primary-text-color, ${FLOW_COLORS.house}); }
        .flow.house { stroke: var(--primary-text-color, ${FLOW_COLORS.house}); }
        .ring.off { stroke: ${FLOW_COLORS.amber} !important; filter: drop-shadow(0 0 6px rgba(255,179,64,0.75)); }
        .big { fill: var(--primary-text-color, #e8eaf0); font-size: 15px; font-weight: 600; text-anchor: middle; }
        .small { fill: var(--primary-text-color, #e8eaf0); font-size: 11px; font-weight: 600; text-anchor: middle; }
        .lbl { fill: var(--secondary-text-color, ${FLOW_COLORS.muted}); font-size: 12px; text-anchor: middle; }
        .lbl.s { font-size: 10.5px; }
        .tag rect { stroke-width: 1.5; fill: var(--primary-background-color, #111418); }
        .tag text { font-size: 13px; font-weight: 600; text-anchor: middle; }
        .foot { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px; color: var(--secondary-text-color, ${FLOW_COLORS.muted}); font-size: 12.5px; }
        .foot b { color: var(--primary-text-color, #e8eaf0); }
        .today { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }
        @media (max-width: 760px) { .today { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
        .stat { border: 1px solid ${FLOW_COLORS.line}; border-radius: 12px; padding: 10px 12px; display: flex; flex-direction: column; gap: 3px; }
        .stat .k { font-size: 11px; color: var(--secondary-text-color, ${FLOW_COLORS.muted}); text-transform: uppercase; letter-spacing: 0.08em; }
        .stat .v { font-size: 22px; font-weight: 600; }
        .stat .v small { font-size: 12px; color: var(--secondary-text-color, ${FLOW_COLORS.muted}); font-weight: 400; margin-left: 2px; }
        .stat.sun .v { color: ${FLOW_COLORS.solar}; } .stat.bat .v { color: ${FLOW_COLORS.battery}; } .stat.grd .v { color: ${FLOW_COLORS.grid}; }
        .tablecol { min-width: 0; }
        .empty { color: var(--secondary-text-color, ${FLOW_COLORS.muted}); padding: 24px; }
      </style>
      <ha-card>
        <div class="grid">
          <div class="flowcol" id="flowcol">
            <div class="head"><h2>Now <span id="time"></span></h2><div class="pills"><span class="pill sun" id="sun" hidden></span><span class="pill ok" id="plan" hidden></span></div></div>
            <svg id="svg" viewBox="0 0 860 690" role="img" aria-label="Live power flow"><g id="ghosts"></g><g id="flows"></g><g id="nodes"></g><g id="tags"></g></svg>
            <div class="foot"><span id="next"></span><span id="src"></span></div>
          </div>
          <div class="tablecol" id="tablecol"></div>
        </div>
        <div class="today" id="today"></div>
      </ha-card>`;
    // The Control Panel card's own table.
    if (customElements.get("nimbus-dispatch-card-v4")) this._mountTable();
    else customElements.whenDefined("nimbus-dispatch-card-v4").then(() => this._mountTable());
    const col = this.shadowRoot.getElementById("flowcol");
    if (window.ResizeObserver) {
      new ResizeObserver(() => this._sizeTable()).observe(col);
    }
  }

  _mountTable() {
    if (this._table) return;
    const t = document.createElement("nimbus-dispatch-card-v4");
    t.setConfig({ table_only: true });
    if (this._hass) t.hass = this._hass;
    this.shadowRoot.getElementById("tablecol").appendChild(t);
    this._table = t;
    this._sizeTable();
  }

  _sizeTable() {
    if (!this._table) return;
    const h = this.shadowRoot.getElementById("flowcol").getBoundingClientRect().height;
    // The heading, legend and Solve Now take about 150 px of the column.
    if (h > 300) this._table.style.setProperty("--nimbus-ftable-height", Math.round(h - 150) + "px");
  }

  _el(tag, attrs, parent) {
    const e = document.createElementNS(FLOW_SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }

  _seg(a, b) {
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1, ux = dx / d, uy = dy / d;
    return { x1: a.x + ux * (a.r + 4), y1: a.y + uy * (a.r + 4), x2: b.x - ux * (b.r + 4), y2: b.y - uy * (b.r + 4) };
  }

  _line(g, p, kw, colour, scale, cls) {
    const w = Math.min(10, (2.2 + Math.sqrt(kw) * 1.3) * (scale || 1));
    const extra = cls ? " " + cls : "";
    this._el("line", { ...p, class: "flow" + extra, stroke: colour, "stroke-width": w, opacity: 0.16 }, g);
    const d = this._el("line", { ...p, class: "flow run" + extra, stroke: colour, "stroke-width": w, "stroke-dasharray": "1 15" }, g);
    d.style.animationDuration = Math.max(0.35, 2.2 / Math.sqrt(Math.max(kw, 0.05))) + "s";
  }

  _render() {
    this._lastRender = Date.now();
    if (!this.shadowRoot || !this._hass) return;
    const root = this.shadowRoot;
    if (!this._state("sensor.nimbus_solver_config")) {
      root.getElementById("time").textContent = "— Nimbus is not set up yet";
      return;
    }
    const m = this._model();
    const { pos, H, W } = this._layout(m);
    const svg = root.getElementById("svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

    const ts = m.p0 ? new Date(m.p0.time) : new Date();
    root.getElementById("time").textContent = ts.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

    // Plan and solar pills.
    const planPill = root.getElementById("plan");
    planPill.hidden = !m.p0;
    planPill.className = "pill " + (m.offPlan ? "off" : "ok");
    planPill.textContent = m.offPlan ? "Off plan" : "On plan";
    const sun = root.getElementById("sun");
    const fcSolar = m.p0 ? parseFloat(m.p0.solar_kw) || 0 : 0;
    if (fcSolar > 0.5) {
      const pct = Math.round((m.solarTotal / fcSolar) * 100);
      sun.hidden = false;
      sun.className = "pill sun" + (pct < 80 ? " low" : "");
      sun.textContent = pct >= 95 && pct <= 110 ? `Solar on forecast (${pct}%)` : `Solar ${pct}% of forecast`;
    } else sun.hidden = true;

    // Next change in the plan, and where prices come from.
    root.getElementById("next").innerHTML = this._nextChange(m.plan);
    root.getElementById("src").textContent = m.p0 && m.p0.import_price_source ? `Prices: ${m.p0.import_price_source === "primary" ? "your price provider" : m.p0.import_price_source}` : "";

    const G = {};
    for (const id of ["ghosts", "flows", "nodes", "tags"]) { G[id] = root.getElementById(id); G[id].replaceChildren(); }
    const C = FLOW_COLORS;

    for (const g of m.ghosts) if (pos[g.from] && pos[g.to]) this._el("line", { ...this._seg(pos[g.from], pos[g.to]), class: "ghost" }, G.ghosts);
    for (const f of m.flows) if (pos[f.from] && pos[f.to]) this._line(G.flows, this._seg(pos[f.from], pos[f.to]), f.kw, C[f.colour]);

    // Circuits in a fan under House (placed by _layout).
    const cs = m.circuits, Hn = pos.house;
    cs.forEach((c, i) => {
      const { x, y } = pos["cir" + i];
      const r = 12 + Math.sqrt(c.kw) * 9;
      this._line(G.flows, this._seg(Hn, { x, y, r }), c.kw, C.house, 0.55, "house");
      this._el("circle", { cx: x, cy: y, r, class: "ring", stroke: C.muted, "stroke-width": 1.5 }, G.nodes);
      this._el("text", { x, y: y + 4, class: "small" }, G.nodes).textContent = c.kw >= 1 ? c.kw.toFixed(1) : Math.round(c.kw * 1000);
      this._el("text", { x, y: y + r + 12, class: "lbl s" }, G.nodes).textContent = c.name + (c.kw >= 1 ? " kW" : " W");
    });

    // Strings.
    m.site.strings.forEach((s, j) => {
      const p = pos["str" + j];
      const kw = this._kw(s.entity);
      if (!p || !(kw > FLOW_MIN_KW)) return;
      this._el("circle", { cx: p.x, cy: p.y, r: p.r, class: "ring", stroke: C.solar }, G.nodes);
      this._el("text", { x: p.x, y: p.y + 4, class: "small" }, G.nodes).textContent = kw.toFixed(1);
      this._el("text", { x: p.x, y: p.y - p.r - 6, class: "lbl s" }, G.nodes).textContent = s.label;
    });

    // Towers: level arc, health underneath.
    m.site.towers.forEach((t, j) => {
      const p = pos["tow" + j];
      if (!p) return;
      const soc = this._num(t.soc), soh = this._num(t.soh), temp = this._num(t.temp);
      this._el("circle", { cx: p.x, cy: p.y, r: p.r, class: "ring", stroke: C.battery }, G.nodes);
      if (!isNaN(soc)) {
        const r2 = p.r - 6, c = 2 * Math.PI * r2;
        this._el("circle", { cx: p.x, cy: p.y, r: r2, fill: "none", stroke: C.line, "stroke-width": 4 }, G.nodes);
        this._el("circle", { cx: p.x, cy: p.y, r: r2, fill: "none", stroke: C.battery, "stroke-width": 4, opacity: 0.6,
          "stroke-dasharray": `${(c * soc) / 100} ${c}`, transform: `rotate(-90 ${p.x} ${p.y})` }, G.nodes);
      }
      this._el("text", { x: p.x, y: p.y + 4, class: "small" }, G.nodes).textContent = isNaN(soc) ? "—" : Math.round(soc) + "%";
      this._el("text", { x: p.x, y: p.y + p.r + 14, class: "lbl s" }, G.nodes).textContent = t.title.replace(/^.*·\s*/, "");
      const extra = [isNaN(soh) ? null : "health " + Math.round(soh) + "%", isNaN(temp) ? null : Math.round(temp) + "°C"].filter(Boolean).join(" · ");
      if (extra) this._el("text", { x: p.x, y: p.y + p.r + 27, class: "lbl s" }, G.nodes).textContent = extra;
    });

    // EVs: SoC arc, power and name underneath.
    m.evs.forEach((e, k) => {
      const p = pos["ev" + k];
      this._el("circle", { cx: p.x, cy: p.y, r: p.r, class: "ring", stroke: C.ev }, G.nodes);
      if (!isNaN(e.socPct)) {
        const r2 = p.r - 6, c = 2 * Math.PI * r2;
        this._el("circle", { cx: p.x, cy: p.y, r: r2, fill: "none", stroke: C.line, "stroke-width": 4 }, G.nodes);
        this._el("circle", { cx: p.x, cy: p.y, r: r2, fill: "none", stroke: C.ev, "stroke-width": 4, opacity: 0.6,
          "stroke-dasharray": `${(c * e.socPct) / 100} ${c}`, transform: `rotate(-90 ${p.x} ${p.y})` }, G.nodes);
      }
      this._el("text", { x: p.x, y: p.y + 4, class: "small" }, G.nodes).textContent = isNaN(e.socPct) ? "—" : Math.round(e.socPct) + "%";
      this._el("text", { x: p.x, y: p.y + p.r + 14, class: "lbl s" }, G.nodes).textContent = e.name;
      const what = e.charge > FLOW_MIN_KW ? "charging " + flowKwText(e.charge) : e.charge < -FLOW_MIN_KW ? "supplying " + flowKwText(e.charge) : "idle";
      this._el("text", { x: p.x, y: p.y + p.r + 27, class: "lbl s" }, G.nodes).textContent = what;
    });

    // Inverters, House, Grid.
    m.invs.forEach((inv, k) => {
      const p = pos["inv" + k];
      const ring = this._el("circle", { cx: p.x, cy: p.y, r: p.r, class: "ring", stroke: inv.batt > 0.05 && inv.batt >= inv.solar ? C.battery : C.solar }, G.nodes);
      if (m.offPlan && Math.abs(inv.batt) >= 0) ring.classList.add("off");
      const ac = inv.solar + inv.batt;
      this._el("text", { x: p.x, y: p.y + 5, class: "big" }, G.nodes).textContent = Math.abs(ac) > FLOW_MIN_KW ? flowKwText(ac) : "0";
      this._el("text", { x: p.x, y: p.y + p.r + 17, class: "lbl" }, G.nodes).textContent = inv.name;
    });
    for (const [id, label, kw, colour] of [["house", "House", m.house, C.house], ["grid", "Grid", Math.abs(m.grid), C.grid]]) {
      const p = pos[id];
      this._el("circle", { cx: p.x, cy: p.y, r: p.r, class: id === "house" ? "ring house" : "ring", stroke: colour }, G.nodes);
      this._el("text", { x: p.x, y: p.y + 5, class: "big" }, G.nodes).textContent = kw > FLOW_MIN_KW ? flowKwText(kw) : "0";
      this._el("text", { x: p.x, y: p.y + p.r + 17, class: "lbl" }, G.nodes).textContent = label;
    }

    // The price on the grid: what is being paid or earned right now.
    if (m.p0) {
      const buy = (parseFloat(m.p0.import_price) || 0) * 100;
      const sell = (parseFloat(m.p0.export_price) || 0) * 100;
      const bonus = (parseFloat(m.p0.bonus_price) || 0) * 100;
      let text, colour;
      if (m.grid < -FLOW_MIN_KW) {
        text = bonus > 1 ? `Selling ${(sell + bonus).toFixed(1)}c · P2P` : `Selling ${sell.toFixed(1)}c`;
        colour = bonus > 1 ? C.p2p : C.solar;
      } else if (m.grid > FLOW_MIN_KW) { text = `Buying ${buy.toFixed(1)}c`; colour = C.grid; }
      else { text = `Buy ${buy.toFixed(1)}c · Sell ${sell.toFixed(1)}c`; colour = C.muted; }
      const g = this._el("g", { class: "tag" }, G.tags);
      const w = 18 + text.length * 7.3, p = pos.grid;
      this._el("rect", { x: p.x - w / 2, y: p.y - p.r - 40, width: w, height: 26, rx: 13, stroke: colour }, g);
      this._el("text", { x: p.x, y: p.y - p.r - 22, fill: colour }, g).textContent = text;
    }

    this._renderToday(m.site.switchboard);
    this._sizeTable();
  }

  _nextChange(plan) {
    if (!plan || plan.length < 2) return "";
    const kind = (p) => {
      const b = parseFloat(p.battery_kw) || 0;
      const exp = parseFloat(p.grid_export_kw) || 0;
      const imp = parseFloat(p.grid_import_kw) || 0;
      const bonus = parseFloat(p.bonus_price) || 0;
      if (b > 0.5 && exp > 0.5 && bonus > 0.01) return ["p2p", `P2P export, ${exp.toFixed(1)} kW`];
      if (b > 0.5 && exp > 0.5) return ["sell", `battery sells ${exp.toFixed(1)} kW`];
      if (b > 0.5) return ["cover", "battery covers the house"];
      if (b < -0.5 && imp > 0.5) return ["gridcharge", "grid charge"];
      if (b < -0.5) return ["solarcharge", "solar charge"];
      return ["idle", "battery idle"];
    };
    const now = kind(plan[0])[0];
    for (const p of plan.slice(1)) {
      const [k, text] = kind(p);
      if (k !== now) {
        const t = new Date(p.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
        return `Next: <b>${flowEscape(t)}</b> ${flowEscape(text)}`;
      }
    }
    return "";
  }

  _renderToday(sb) {
    const tiles = [
      ["Solar made", sb.switchboard_solar_energy_daily_sensor, "sun"],
      ["House used", sb.switchboard_house_load_energy_daily_sensor, ""],
      ["Imported", sb.switchboard_import_energy_daily_sensor, "grd"],
      ["Exported", sb.switchboard_export_energy_daily_sensor, "grd"],
      ["Battery in", sb.switchboard_battery_charge_daily_sensor, "bat"],
      ["Battery out", sb.switchboard_battery_discharge_daily_sensor, "bat"],
    ].filter(([, id]) => id && !isNaN(this._num(id)));
    const box = this.shadowRoot.getElementById("today");
    box.hidden = !tiles.length;
    box.innerHTML = tiles.map(([k, id, cls]) => {
      const s = this._state(id);
      const unit = flowEscape((s.attributes || {}).unit_of_measurement || "kWh");
      return `<div class="stat ${cls}"><span class="k">${flowEscape(k)}</span><span class="v">${this._num(id).toFixed(1)}<small>${unit}</small></span></div>`;
    }).join("");
  }
}

// nimbus #1601: register into whichever registry Home Assistant reads, before
// and after it swaps in its scoped registry (same helper as the other cards).
function nimbusRegisterCard(name, cls) {
  const ensure = () => {
    const registry = window.customElements;
    if (!registry || registry.get(name)) return;
    try {
      registry.define(name, class extends cls {});
    } catch (_e) {
      // registered meanwhile by another copy of this card: nothing to do
    }
  };
  try {
    customElements.define(name, cls);
  } catch (_e) {
    // already registered (an older copy of this card loaded first)
  }
  for (const ms of [0, 100, 500, 1500, 5000, 15000]) setTimeout(ensure, ms);
  window.addEventListener("load", ensure, { once: true });
}

nimbusRegisterCard("nimbus-flow-card", NimbusFlowCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "nimbus-flow-card",
  name: "Nimbus Power Flow",
  description:
    "Live animated power flow through your inverters, strings and battery towers, the plan's current price and whether it is being followed, with the Control Panel's forecast table and today's totals.",
});
