// nimbus-solar-card.js -- the Solar Lab tab
//
// Nimbus's own solar forecast (sensor.nimbus_solar_forecast, solar_forecaster_ha.py) as an instrument
// panel for one roof: a header strip, four headline numbers, the forecast chart with today's measured
// output, a table of every day, the next two hours, the strings as learned, and its own scoreboard.
// Two columns that stack on a phone. Plain canvas charts drawn at the card's real size (never
// stretched), redrawn when the card is resized. Amber for sun, slate for sky; light and dark themes.

const NSC_CSS = `
:host{display:block;--sun:#d98a00;--band:rgba(217,138,0,.18);--clear:#8a94a6;--act:#1d6fd8;--good:#1f7a4d;--bad:#b3261e;
  --mono:ui-monospace,"IBM Plex Mono",Consolas,"Roboto Mono",monospace;--fg:var(--primary-text-color);--dim:var(--secondary-text-color);
  --line:var(--divider-color,rgba(127,127,127,.25))}
:host([dark]){--sun:#ffb347;--band:rgba(255,179,71,.18);--clear:#7d8799;--act:#5aa2ff;--good:#5fd39a;--bad:#ff7a70}
.wrap{container-type:inline-size;display:grid;grid-template-columns:minmax(0,1fr);gap:12px;min-width:0}
.wrap>*{min-width:0}
.top{display:flex;flex-wrap:wrap;gap:4px 18px;align-items:baseline;border-bottom:2px solid var(--sun);padding-bottom:8px}
.top h1{font-size:26px;font-weight:700;line-height:1;margin:0;color:var(--fg)}
.top span{color:var(--dim);font:12.5px var(--mono)}
.hero{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.two{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:12px}
.card{background:var(--ha-card-background,var(--card-background-color));border:1px solid var(--line);border-radius:8px;padding:14px 16px;min-width:0}
.k{font:500 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.v{font-size:32px;font-weight:700;line-height:1.15;font-variant-numeric:tabular-nums;color:var(--fg)}
.v small{font-size:14px;font-weight:500;color:var(--dim);margin-left:4px}
.s{color:var(--dim);font-size:13px}
h2{font-size:17px;font-weight:700;margin:0 0 8px;color:var(--fg)}
.cbox{position:relative;height:300px}.cbox.sm{height:190px}
canvas{position:absolute;inset:0;width:100%;height:100%}
.leg{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:6px;font-size:12px;color:var(--dim)}
.leg i{display:inline-block;width:12px;height:3px;margin-right:5px;vertical-align:3px}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13.5px;color:var(--fg)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font:500 11px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--dim)}
td:first-child,th:first-child{text-align:left}
tr:last-child td{border-bottom:0}
.g{color:var(--good);font-weight:600}.r{color:var(--bad)}
.act{color:var(--sun);font-weight:700}th.act{color:var(--sun)}
.bar{display:inline-block;height:6px;border-radius:3px;background:var(--sun);vertical-align:1px}
.why{font-size:12px;color:var(--dim)}
.note{font-size:13px;color:var(--dim);max-width:75ch;margin:8px 0 0}
@container (max-width:860px){.hero{grid-template-columns:repeat(2,minmax(0,1fr))}.two{grid-template-columns:minmax(0,1fr)}.v{font-size:26px}}
`;

class NimbusSolarCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { entity: "sensor.nimbus_solar_forecast", ...(config || {}) };
  }

  getCardSize() {
    return 14;
  }

  getGridOptions() {
    return { columns: "full", rows: "auto" };
  }

  connectedCallback() {
    if (!this._ro && window.ResizeObserver) {
      let t = 0;
      this._ro = new ResizeObserver(() => {
        clearTimeout(t);
        t = setTimeout(() => this._charts(), 120);
      });
      this._ro.observe(this);
    }
  }

  disconnectedCallback() {
    if (this._ro) this._ro.disconnect();
    this._ro = null;
  }

  set hass(hass) {
    this._hass = hass;
    const dark = !!(hass.themes && hass.themes.darkMode);
    if (dark !== this.hasAttribute("dark")) this.toggleAttribute("dark", dark);
    const st = hass.states[this._config.entity];
    const ys = (st && st.attributes && st.attributes.yardsticks) || {};
    const ids = Object.values(ys).flatMap((d) => Object.values(d).flat());
    const key = (st ? st.last_updated + "|" + st.state : "none") + "|" + dark + "|" +
      ids.map((i) => (hass.states[i] ? hass.states[i].state : "")).join(",");
    if (key === this._key) return;
    this._key = key;
    this._st = st;
    this._render();
  }

  // ---- helpers ------------------------------------------------------------------------------
  _esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  _tz() {
    return (this._hass && this._hass.config && this._hass.config.time_zone) || undefined;
  }

  _day(d) {
    return new Date(d + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }

  _num(v, k = 1) {
    return v == null || isNaN(+v) ? "–" : (+v).toFixed(k);
  }

  // the other solar forecasts installed (found by Nimbus, no configuration): their live total for the
  // day `ahead` days from today, summed over their entries (one per panel plane); null if not offered
  _other(name, ahead) {
    const ys = (this._st && this._st.attributes && this._st.attributes.yardsticks) || {};
    const ids = (ys[name] || {})[String(ahead)];
    if (!ids || !ids.length) return null;
    let sum = 0;
    for (const id of ids) {
      const s = this._hass.states[id];
      const v = s ? parseFloat(s.state) : NaN;
      if (!isFinite(v)) return null;
      sum += s.attributes && s.attributes.unit_of_measurement === "Wh" ? v / 1000 : v;
    }
    return sum;
  }

  _ahead(date, todayIso) {
    return Math.round((Date.parse(date + "T12:00:00") - Date.parse(todayIso + "T12:00:00")) / 864e5);
  }

  _models(m) {
    return this._esc(String(m || "").toUpperCase().replace(/\+/g, " + "));
  }

  // ---- page ---------------------------------------------------------------------------------
  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const st = this._st;
    if (!st) {
      this.shadowRoot.innerHTML = `<style>${NSC_CSS}</style><ha-card><div class="card"><h2>Solar Lab</h2>
        <p class="note">Nimbus's own solar forecast is switched off. Turn it on: Settings → Devices &amp; services →
        Nimbus → Configure → Solar forecaster.</p></div></ha-card>`;
      return;
    }
    const a = st.attributes || {};
    const tz = this._tz();
    const days = Array.isArray(a.days) ? a.days : [];
    const today = a.today && a.today.date ? a.today : null;
    const score = a.score || {};
    const rows = Array.isArray(a.scoreboard) ? a.scoreboard : [];
    const arrays = Array.isArray(a.arrays) ? a.arrays : [];
    const statusText = {
      starting: "starting",
      fetching_history: "fetching weather history",
      learning: "learning your panels",
      ready: "ready",
      no_arrays: "no PV Strings yet",
      error: "needs attention",
    }[a.status] || a.status || "";

    // header strip
    const made = a.made_at
      ? new Date(a.made_at).toLocaleString(undefined, { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false })
      : "–";
    const runs = a.weather_runs || {};
    const src = ["ecmwf", "gefs", "gfs"].filter((k) => runs[k]).map((k) => `${k.toUpperCase()} ${this._esc(runs[k])}`).join(" · ");
    const live = a.live_correction == null ? "live correction: waiting for sun" : `live correction ×${(+a.live_correction).toFixed(2)}`;
    const top = `<div class="top"><h1>Solar Lab</h1><span>forecast ${this._esc(made)}</span><span>weather: ${src || "–"}</span>
      <span>${this._esc(live)}</span><span>${this._esc(statusText)}${a.status_detail ? " · " + this._esc(a.status_detail) : ""}</span></div>`;

    // hero
    const tom = days[0];
    const sum = (k) => days.reduce((s, d) => s + (+d[k] || 0), 0);
    const now = st.state && !isNaN(+st.state) ? (+st.state).toFixed(1) : "–";
    const hero = `<div class="hero">
      <div class="card"><div class="k">Today</div><div class="v">${today ? this._num(today.p50) : "–"}<small>kWh</small></div>
        <div class="s">${today ? `made so far ${this._num(today.made_so_far_kwh)} · rest ${this._num(today.rest_kwh)} · likely ${this._num(today.p10)}–${this._num(today.p90)}` : "the rest of today appears with the first forecast"} · now ${now} kW</div></div>
      <div class="card"><div class="k">Tomorrow</div><div class="v">${tom ? this._num(tom.p50) : "–"}<small>kWh</small></div>
        <div class="s">${tom ? `${this._day(tom.date)} · p10–p90 ${this._num(tom.p10)}–${this._num(tom.p90)} · clear ${this._num(tom.clear)}` : "–"}</div></div>
      <div class="card"><div class="k">Next ${days.length || "–"} days</div><div class="v">${days.length ? sum("p50").toFixed(0) : "–"}<small>kWh</small></div>
        <div class="s">clear-sky most ${days.length ? sum("clear").toFixed(0) : "–"} kWh</div></div>
      <div class="card"><div class="k">Day-ahead error · ${score.scored_days ?? "–"} days</div><div class="v">${this._num(score.daily_error_pct)}<small>%</small></div>
        <div class="s">range holds ${score.p10_p90_cover != null ? Math.round(score.p10_p90_cover * 100) : "–"}% of readings · weeks it never saw</div></div>
    </div>`;

    // main chart
    const main = `<div class="card"><h2>Today and the next ${days.length || ""} days</h2><div class="why" style="margin:-4px 0 6px">every 15 minutes for two days, hourly after · today's measured output from midnight</div>
      <div class="cbox"><canvas id="cMain"></canvas></div>
      <div class="leg"><span><i style="background:var(--sun)"></i>forecast (p50)</span><span><i style="background:var(--band);height:9px"></i>p10–p90</span>
      <span><i style="background:var(--clear)"></i>clear sky</span><span><i style="background:var(--act)"></i>measured</span></div></div>`;

    // days table: ours, then every other solar forecast installed, for comparison
    const others = Object.keys(a.yardsticks || {});
    const todayIso = today ? today.date : new Date().toLocaleDateString("en-CA", { timeZone: tz });
    const allDays = (today ? [{ ...today, today: true }] : []).concat(days);
    const dayRows = allDays.map((d) => {
      const kt = d.clear > 0 ? d.p50 / d.clear : 0;
      const ahead = this._ahead(d.date, todayIso);
      const oc = others.map((n) => `<td>${this._num(this._other(n, ahead))}</td>`).join("");
      return `<tr><td>${d.today ? "Today" : this._esc(this._day(d.date))}</td><td><b>${this._num(d.p50)}</b></td><td>${this._num(d.p10)}–${this._num(d.p90)}</td>${oc}
        <td><span class="bar" style="width:${Math.round(60 * Math.min(kt, 1.1))}px"></span></td><td>${kt.toFixed(2)}</td><td class="why">${this._models(d.model)}</td></tr>`;
    }).join("");
    const othersNote = others.length
      ? " " + others.map((n) => this._esc(n)).join(", ") + ": the other solar forecasts installed here, found automatically and shown for comparison only; Nimbus never uses them."
      : "";
    const dayCard = `<div class="card"><h2>${allDays.length ? allDays.length + " days" : "Days"}</h2><div class="tw"><table>
      <tr><th>day</th><th>ours</th><th>p10–p90</th>${others.map((n) => `<th>${this._esc(n)}</th>`).join("")}<th></th><th>kt</th><th>weather</th></tr>${dayRows}</table></div>
      <p class="why">kWh. kt is how much of a clear day it expects (1.0 = clear). Weather shows which models fed the day: past about
      two and a half days ECMWF and GEFS have ended and GFS alone carries it, with a wider range.${othersNote}</p></div>`;

    // next two hours + strings
    const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
    const strRows = arrays.map((x) => `<tr><td>${this._esc(x.label || x.entity_id)}${x.active === false ? ' <span class="why">(unused)</span>' : ""}</td>
      <td>${x.kwp ? (+x.kwp).toFixed(2) : "–"}</td><td>${x.tilt != null ? Math.round(x.tilt) + "°" : "–"}${x.orientation === "set by you" ? ' <span class="why">set by you</span>' : ""}</td>
      <td>${x.azimuth != null ? Math.round(x.azimuth) + "° " + dirs[Math.round(x.azimuth / 45) % 8] : "–"}</td></tr>`).join("");
    const side = `<div class="card"><h2>Next two hours</h2><div class="cbox sm"><canvas id="cMin"></canvas></div>
      <p class="note">Every 15 minutes, corrected from the last 30 minutes measured${a.live_correction != null ? ` (×${(+a.live_correction).toFixed(2)})` : ""}.</p>
      <h2 style="margin-top:14px">Your strings, as learned</h2><div class="tw"><table><tr><th>string</th><th>kWp</th><th>tilt</th><th>faces</th></tr>${strRows}
      <tr><td colspan="4" class="why">haze (Linke turbidity) ${this._num(a.haze, 2)}</td></tr></table></div></div>`;

    // scoreboards
    const cell = (v, act) => {
      if (v == null) return "<td>–</td>";
      const e = Math.abs(v - act) / Math.max(act, 1);
      return `<td class="${e < 0.1 ? "g" : e > 0.3 ? "r" : ""}">${(+v).toFixed(1)}</td>`;
    };
    const sRows = rows.slice().reverse().map((r) => `<tr><td>${this._esc(this._day(r.date))}</td><td class="act">${this._num(r.actual_kwh)}</td>
      ${cell(r.forecast_kwh, r.actual_kwh)}${others.map((n) => cell((r.others || {})[n], r.actual_kwh)).join("")}<td>${this._num(r.p10_kwh)}–${this._num(r.p90_kwh)}</td>
      <td>${r.actual_kwh >= r.p10_kwh && r.actual_kwh <= r.p90_kwh ? "✓" : '<span class="r">✗</span>'}</td></tr>`).join("");
    let summary = "";
    if (rows.length) {
      const m = rows.reduce((s, r) => s + r.actual_kwh, 0) / rows.length;
      const e = rows.map((r) => r.forecast_kwh - r.actual_kwh);
      const mae = e.reduce((s, x) => s + Math.abs(x), 0) / e.length;
      const b = e.reduce((s, x) => s + x, 0) / e.length;
      const inside = rows.filter((r) => r.actual_kwh >= r.p10_kwh && r.actual_kwh <= r.p90_kwh).length;
      summary = `<tr><td><b>ours</b></td><td>${rows.length}</td><td>${mae.toFixed(1)} kWh</td><td>${((100 * mae) / m).toFixed(1)}%</td>
        <td>${b >= 0 ? "+" : ""}${b.toFixed(1)}</td><td>${Math.round((100 * inside) / rows.length)}%</td></tr>`;
      for (const n of others) {
        const rs = rows.filter((r) => (r.others || {})[n] != null);
        if (!rs.length) {
          summary += `<tr><td>${this._esc(n)}</td><td>0</td><td colspan="4" class="why">no evening-before figures in the recorder yet</td></tr>`;
          continue;
        }
        const mo = rs.reduce((t, r) => t + r.actual_kwh, 0) / rs.length;
        const eo = rs.map((r) => r.others[n] - r.actual_kwh);
        const maeo = eo.reduce((t, x) => t + Math.abs(x), 0) / eo.length;
        const bo = eo.reduce((t, x) => t + x, 0) / eo.length;
        summary += `<tr><td>${this._esc(n)}</td><td>${rs.length}</td><td>${maeo.toFixed(1)} kWh</td><td>${((100 * maeo) / mo).toFixed(1)}%</td>
          <td>${bo >= 0 ? "+" : ""}${bo.toFixed(1)}</td><td>–</td></tr>`;
      }
    }
    const board = `<div class="two">
      <div class="card"><h2>Scoreboard · evening-before forecasts, every day</h2><div class="tw"><table>
        <tr><th>day</th><th class="act">actual</th><th>ours</th>${others.map((n) => `<th>${this._esc(n)}</th>`).join("")}<th>p10–p90</th><th>in range</th></tr>${sRows || '<tr><td colspan="5" class="why">after the first training</td></tr>'}</table></div>
        <p class="note">Each day forecast from the weather runs published the evening before, by a model trained without that week. Green within 10 %, red off by more than 30 %.</p></div>
      <div class="card"><h2>Scoreboard · summary</h2><div class="tw"><table>
        <tr><th>forecast</th><th>days</th><th>error</th><th>%</th><th>bias</th><th>in range</th></tr>${summary}</table></div>
        <p class="note">Daily kWh error against what your PV Strings measured. Retrained every night on the latest weeks.${others.length
          ? " The other solar forecasts installed here are found automatically and judged on their tomorrow total as it stood at 21:00 the evening before; yardsticks only, Nimbus never uses them."
          : ""}</p></div></div>`;

    const how = `<div class="card"><h2>How it works</h2><p class="note">Every evening the three best free global weather models are saved:
      ECMWF (the European centre's model), NOAA's GFS and NOAA's 31-run GEFS ensemble. Nothing needs a key, and your location is never
      sent anywhere: the files are global and Nimbus picks your point out of them. Each 5 minutes: the sun's exact position, the clear-sky
      light on each PV String at the angle you gave it (or the angle it learned), panel heat loss, then a quantile gradient-boosted model
      trained on your own measured output turns the models' cloud and light into p10 / p50 / p90 kW. The range is widened until 80 % of
      real readings fall inside it. Through the day the last 30 minutes measured correct the next hours. Scored on weeks the model never saw.</p>
      <p class="why">${this._esc(a.attribution || "")}</p></div>`;

    this.shadowRoot.innerHTML = `<style>${NSC_CSS}</style><ha-card style="background:none;border:none;box-shadow:none">
      <div class="wrap">${top}${hero}${main}<div class="two">${dayCard}${side}</div>${board}${how}</div></ha-card>`;
    this._charts();
  }

  // ---- charts -------------------------------------------------------------------------------
  _cv(id) {
    const c = this.shadowRoot && this.shadowRoot.getElementById(id);
    if (!c) return null;
    const box = c.parentElement;
    const r = window.devicePixelRatio || 1;
    const w = box.clientWidth;
    const h = box.clientHeight;
    if (!w || !h) return null;
    c.width = w * r;
    c.height = h * r;
    const x = c.getContext("2d");
    x.setTransform(r, 0, 0, r, 0, 0);
    x.clearRect(0, 0, w, h);
    return { x, w, h };
  }

  _var(n) {
    return getComputedStyle(this).getPropertyValue(n).trim() || "#888";
  }

  _axes(x, w, h, L, B, ym, t0, t1, step) {
    const tz = this._tz();
    x.strokeStyle = this._var("--line");
    x.fillStyle = this._var("--dim");
    x.font = "11px ui-monospace, Consolas, monospace";
    x.lineWidth = 1;
    const vs = ym > 8 ? 4 : ym > 3 ? 1 : 0.5;
    for (let v = 0; v <= ym + 1e-9; v += vs) {
      const Y = h - B - ((h - B - 8) * v) / ym;
      x.beginPath();
      x.moveTo(L, Y);
      x.lineTo(w, Y);
      x.stroke();
      x.fillText(String(+v.toFixed(1)), 2, Y + 4);
    }
    for (let s = Math.ceil(t0 / step) * step; s <= t1; s += step) {
      const X = L + ((w - L) * (s - t0)) / (t1 - t0);
      x.beginPath();
      x.moveTo(X, 8);
      x.lineTo(X, h - B);
      x.stroke();
      const lab = new Date(s * 1000).toLocaleString(undefined, {
        timeZone: tz,
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        ...(step >= 21600 ? { weekday: "short" } : {}),
      });
      if (X + x.measureText(lab).width + 6 <= w) x.fillText(lab, X + 3, h - 4); // never cut at the edge
    }
  }

  _line(x, X, Y, ts, vs, colour, width, dash) {
    x.beginPath();
    x.setLineDash(dash || []);
    let started = false;
    ts.forEach((s, k) => {
      const v = vs[k];
      if (v == null || isNaN(v)) {
        started = false;
        return;
      }
      if (started) x.lineTo(X(s), Y(v));
      else {
        x.moveTo(X(s), Y(v));
        started = true;
      }
    });
    x.strokeStyle = colour;
    x.lineWidth = width;
    x.stroke();
    x.setLineDash([]);
  }

  _series() {
    const a = (this._st && this._st.attributes) || {};
    const f = (Array.isArray(a.forecast) ? a.forecast : []).map((r) => ({ ...r, t: Date.parse(r.time) / 1000 }));
    const m = (Array.isArray(a.measured_today) ? a.measured_today : []).map((r) => ({ t: Date.parse(r.time) / 1000, v: r.value }));
    return { f, m, live: a.live_correction };
  }

  _charts() {
    if (!this._st) return;
    const { f, m } = this._series();
    this._mainChart(f, m);
    this._miniChart(f, m);
  }

  _mainChart(f, m) {
    const c = this._cv("cMain");
    if (!c) return;
    const { x, w, h } = c;
    const L = 28;
    const B = 18;
    if (f.length < 2) {
      x.fillStyle = this._var("--dim");
      x.font = "13px sans-serif";
      x.fillText("The first forecast comes once it has about a week of history.", L, h / 2);
      return;
    }
    const now = f[0].t;
    const t0 = m.length ? Math.min(m[0].t, now) : now;
    const t1 = f[f.length - 1].t;
    const ym = Math.ceil(Math.max(1, ...f.map((r) => Math.max(r.upper || 0, r.clear || 0)), ...m.map((r) => r.v || 0)) / 2) * 2;
    // the finest time step whose labels still have room (about 80 px each), so they never collide
    const step = [21600, 43200, 86400].find((st) => ((w - L) * st) / (t1 - t0) >= 80) || 86400;
    this._axes(x, w, h, L, B, ym, t0, t1, step);
    const X = (s) => L + ((w - L) * (s - t0)) / (t1 - t0);
    const Y = (v) => h - B - ((h - B - 8) * v) / ym;
    x.beginPath();
    f.forEach((r, k) => (k ? x.lineTo(X(r.t), Y(r.upper)) : x.moveTo(X(r.t), Y(r.upper))));
    for (let k = f.length - 1; k >= 0; k--) x.lineTo(X(f[k].t), Y(f[k].lower));
    x.closePath();
    x.fillStyle = this._var("--band");
    x.fill();
    this._line(x, X, Y, f.map((r) => r.t), f.map((r) => r.clear), this._var("--clear"), 1, [4, 4]);
    this._line(x, X, Y, f.map((r) => r.t), f.map((r) => r.value), this._var("--sun"), 2);
    this._line(x, X, Y, m.map((r) => r.t), m.map((r) => r.v), this._var("--act"), 1.6);
    const Xn = X(now);
    x.strokeStyle = this._var("--fg") || "#888";
    x.globalAlpha = 0.4;
    x.beginPath();
    x.moveTo(Xn, 8);
    x.lineTo(Xn, h - B);
    x.stroke();
    x.globalAlpha = 1;
    x.fillStyle = this._var("--dim");
    x.fillText("now", Xn + 3, 18);
    x.fillText("kW", L + 2, 18);
  }

  _miniChart(f, m) {
    const c = this._cv("cMin");
    if (!c) return;
    const { x, w, h } = c;
    const L = 28;
    const B = 18;
    if (!f.length) return;
    const now = f[0].t;
    const t0 = now - 3600;
    const t1 = now + 7200;
    const fw = f.filter((r) => r.t <= t1);
    const mw = m.filter((r) => r.t >= t0);
    const mx = Math.max(0, ...fw.map((r) => r.upper || 0), ...mw.map((r) => r.v || 0));
    if (mx < 0.05) {
      x.fillStyle = this._var("--dim");
      x.font = "13px sans-serif";
      x.fillText("Sun is down: this view starts at sunrise.", L, h / 2);
      return;
    }
    const ym = Math.max(1, Math.ceil(mx * 1.15));
    this._axes(x, w, h, L, B, ym, t0, t1, 1800);
    const X = (s) => L + ((w - L) * (s - t0)) / (t1 - t0);
    const Y = (v) => h - B - ((h - B - 8) * v) / ym;
    x.beginPath();
    fw.forEach((r, k) => (k ? x.lineTo(X(r.t), Y(r.upper)) : x.moveTo(X(r.t), Y(r.upper))));
    for (let k = fw.length - 1; k >= 0; k--) x.lineTo(X(fw[k].t), Y(fw[k].lower));
    x.closePath();
    x.fillStyle = this._var("--band");
    x.fill();
    this._line(x, X, Y, fw.map((r) => r.t), fw.map((r) => r.value), this._var("--sun"), 2);
    this._line(x, X, Y, mw.map((r) => r.t), mw.map((r) => r.v), this._var("--act"), 1.6);
    const Xn = X(now);
    x.strokeStyle = this._var("--fg") || "#888";
    x.globalAlpha = 0.4;
    x.beginPath();
    x.moveTo(Xn, 8);
    x.lineTo(Xn, h - B);
    x.stroke();
    x.globalAlpha = 1;
    x.fillStyle = this._var("--dim");
    x.fillText("now", Xn + 3, 18);
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
  name: "Nimbus Solar Lab",
  description: "Nimbus's own solar forecast: today, each day ahead with its likely range, the forecast chart with today's measured output, the strings as learned and its own scoreboard.",
});
