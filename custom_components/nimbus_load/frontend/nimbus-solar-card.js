// nimbus-solar-card.js
//
// Nimbus's own solar forecast (sensor.nimbus_solar_forecast, solar_forecaster_ha.py) on one plain card
// that reads well on a phone: the expected power now, each day ahead (about four) with its likely range, the
// next 48 hours as a line with its p10-p90 band (and today's measured output where the PV Strings
// report it), what it learned about each array, and its own score. No configuration needed.
//
// Plain HTMLElement + shadow DOM + inline SVG, like this project's other cards; colours come from the
// Home Assistant theme (light and dark).

class NimbusSolarCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { entity: "sensor.nimbus_solar_forecast", hours: 48, ...(config || {}) };
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config.entity];
    const key = st ? st.last_updated + "|" + st.state : "none";
    if (key === this._key) return;
    this._key = key;
    this._render(st);
  }

  _esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  _render(st) {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const css = `
      :host { display: block; }
      ha-card { padding: 16px; }
      .h { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap; }
      .t { font-size: 1.1em; font-weight: 600; }
      .s { color: var(--secondary-text-color); font-size: .85em; }
      .now { font-size: 2.2em; font-weight: 600; line-height: 1.1; }
      .now small { font-size: .45em; font-weight: 400; color: var(--secondary-text-color); margin-left: 4px; }
      .days { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin: 12px 0; }
      .d { border: 1px solid var(--divider-color); border-radius: 8px; padding: 8px 10px; }
      .d b { font-size: 1.3em; }
      svg { width: 100%; height: 150px; display: block; }
      table { width: 100%; border-collapse: collapse; font-size: .85em; margin-top: 10px; }
      td, th { padding: 4px 6px; border-bottom: 1px solid var(--divider-color); text-align: right; white-space: nowrap; }
      td:first-child, th:first-child { text-align: left; white-space: normal; }
      th { font-weight: 500; color: var(--secondary-text-color); }
      .leg { display: flex; gap: 14px; flex-wrap: wrap; font-size: .8em; color: var(--secondary-text-color); margin-top: 4px; }
      .leg i { display: inline-block; width: 12px; height: 3px; margin-right: 5px; vertical-align: middle; }
    `;
    if (!st) {
      this.shadowRoot.innerHTML = `<style>${css}</style><ha-card><div class="t">Solar forecast</div>
        <p class="s">Turn on Nimbus's solar forecaster: Settings → Devices &amp; services → Nimbus → Configure → Solar forecaster.</p></ha-card>`;
      return;
    }
    const a = st.attributes || {};
    const rows = Array.isArray(a.forecast) ? a.forecast : [];
    const days = Array.isArray(a.days) ? a.days : [];
    const tz = (this._hass.config && this._hass.config.time_zone) || undefined;
    const statusText = {
      starting: "Starting",
      fetching_history: "Fetching weather history",
      learning: "Learning your panels",
      ready: "Ready",
      no_arrays: "No PV Strings yet",
      error: "Needs attention",
    }[a.status] || a.status || "";
    const now = st.state && !isNaN(+st.state) ? (+st.state).toFixed(1) : "–";
    const dayName = (d) => new Date(d + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    const iso = (off) => new Date(Date.now() + off * 864e5).toLocaleDateString("en-CA", { timeZone: tz });
    const label = (d) => (d === iso(0) ? "Today" : d === iso(1) ? "Tomorrow" : dayName(d));
    // every day it forecasts (about four); the far ones come from GFS alone, with a wider range
    const dayCards = days.map((d) =>
      `<div class="d"><div class="s">${this._esc(label(d.date))}</div>
        <b>${d.p50}</b> kWh<div class="s">likely ${d.p10}–${d.p90} · clear ${d.clear}</div>${
          d.model === "gfs" ? '<div class="s">GFS only: wider range</div>' : ""}</div>`).join("");
    const score = a.score && a.score.daily_error_pct != null
      ? `<div class="d"><div class="s">Its own score</div><b>${a.score.daily_error_pct}%</b><div class="s">daily error · range holds ${Math.round((a.score.p10_p90_cover || 0) * 100)}%</div></div>`
      : "";

    // chart: next N hours, p50 line over the p10-p90 band
    let svg = "";
    if (rows.length > 1) {
      const t0 = Date.parse(rows[0].time), t1 = t0 + this._config.hours * 3600e3;
      const pts = rows.filter((r) => Date.parse(r.time) <= t1);
      const ymax = Math.max(1, ...pts.map((r) => r.upper || r.value)) * 1.1;
      const W = 600, H = 150, L = 30, B = 18;
      const X = (t) => L + ((W - L - 4) * (Date.parse(t) - t0)) / (t1 - t0);
      const Y = (v) => H - B - ((H - B - 6) * v) / ymax;
      const band = pts.map((r) => `${X(r.time)},${Y(r.upper)}`).join(" ") + " " + pts.slice().reverse().map((r) => `${X(r.time)},${Y(r.lower)}`).join(" ");
      const line = pts.map((r) => `${X(r.time)},${Y(r.value)}`).join(" ");
      let ticks = "";
      for (let k = 0; k <= Math.ceil(ymax); k += Math.max(1, Math.round(ymax / 3))) {
        ticks += `<line x1="${L}" x2="${W}" y1="${Y(k)}" y2="${Y(k)}" stroke="var(--divider-color)"/><text x="2" y="${Y(k) + 4}" font-size="10" fill="var(--secondary-text-color)">${k}</text>`;
      }
      for (let h = 0; h <= this._config.hours; h += 12) {
        const tt = new Date(t0 + h * 3600e3);
        const lab = tt.toLocaleString(undefined, { timeZone: tz, weekday: "short", hour: "2-digit", hour12: false });
        const x = L + ((W - L - 4) * h) / this._config.hours;
        ticks += `<text x="${x + 2}" y="${H - 4}" font-size="10" fill="var(--secondary-text-color)">${this._esc(lab)}</text>`;
      }
      svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Solar forecast, next ${this._config.hours} hours">
        ${ticks}
        <polygon points="${band}" fill="var(--warning-color, #ffa600)" fill-opacity=".18" stroke="none"/>
        <polyline points="${line}" fill="none" stroke="var(--warning-color, #ffa600)" stroke-width="2"/>
      </svg>
      <div class="leg"><span><i style="background:var(--warning-color,#ffa600)"></i>forecast (kW)</span><span><i style="background:var(--warning-color,#ffa600);opacity:.3;height:8px"></i>likely range (p10–p90)</span></div>`;
    } else {
      svg = `<p class="s">${this._esc(a.status_detail || "The first forecast comes once it has about a week of history.")}</p>`;
    }

    const arrays = Array.isArray(a.arrays) ? a.arrays : [];
    const arrTable = arrays.length
      ? `<table><tr><th>Array</th><th>Size</th><th>Tilt</th><th>Faces</th></tr>${arrays.map((x) =>
          `<tr><td>${this._esc(x.label || x.entity_id)}${x.active === false ? " <span class=\"s\">(unused)</span>" : ""}</td>
           <td>${x.kwp ? x.kwp.toFixed(2) + " kW" : "–"}</td><td>${x.tilt != null ? Math.round(x.tilt) + "°" : "–"}</td>
           <td>${x.azimuth != null ? Math.round(x.azimuth) + "°" : "–"} <span class="s">${this._esc(x.orientation || "")}</span></td></tr>`).join("")}</table>`
      : "";

    this.shadowRoot.innerHTML = `<style>${css}</style><ha-card>
      <div class="h"><span class="t">Solar forecast</span><span class="s">${this._esc(statusText)}${a.model ? " · " + this._esc(a.model.toUpperCase().replace(/\+/g, " + ")) : ""}</span></div>
      <div class="now">${now}<small>kW now</small></div>
      <div class="days">${dayCards}${score}</div>
      ${svg}
      ${arrTable}
      <div class="s" style="margin-top:8px">${this._esc(a.attribution || "")}</div>
    </ha-card>`;
  }
}

// Same registration as this project's other cards (nimbus-forecast-card.js): survives HA's scoped
// custom-element registry swap.
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

nimbusRegisterCard("nimbus-solar-card", NimbusSolarCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "nimbus-solar-card",
  name: "Nimbus Solar Forecast",
  description: "Nimbus's own solar forecast: power now, today and tomorrow with their likely range, the next 48 hours, and what it learned about each array.",
});
