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
.wbox{position:relative;height:70px;margin-top:2px;touch-action:pan-y;cursor:crosshair}
.wtip{position:absolute;top:-6px;transform:translate(-50%,-100%);background:var(--ha-card-background,var(--card-background-color,#fff));
  border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:12px;line-height:1.5;color:var(--fg);white-space:nowrap;
  pointer-events:none;box-shadow:0 4px 14px rgba(0,0,0,.22);z-index:2}
.wtip b{font-weight:600}
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

  // Charts are drawn at the card's real size, so they are redrawn whenever that size can have
  // changed or been unknown: after each update once laid out, when the card or a chart box resizes or
  // becomes visible, and when the page comes back to the front. (A chart drawn while the card had no
  // size -- a background tab, another dashboard view -- otherwise stayed blank until a reload.)
  connectedCallback() {
    if (!this._ro && window.ResizeObserver) {
      let t = 0;
      this._ro = new ResizeObserver(() => {
        clearTimeout(t);
        t = setTimeout(() => this._charts(), 120);
      });
      this._ro.observe(this);
    }
    if (!this._onShow) {
      this._onShow = () => {
        if (document.visibilityState === "visible") this._redrawSoon();
      };
      document.addEventListener("visibilitychange", this._onShow);
      window.addEventListener("focus", this._onShow);
    }
    this._redrawSoon();
    this._wxAnimate();
  }

  disconnectedCallback() {
    if (this._ro) this._ro.disconnect();
    this._ro = null;
    clearInterval(this._wxTimer);
    this._wxTimer = null;
    if (this._onShow) {
      document.removeEventListener("visibilitychange", this._onShow);
      window.removeEventListener("focus", this._onShow);
      this._onShow = null;
    }
  }

  _redrawSoon() {
    const draw = () => this._charts();
    if (window.requestAnimationFrame) requestAnimationFrame(draw);
    setTimeout(draw, 300);
    setTimeout(draw, 1500);
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
    try {
      this._render();
    } catch (err) {
      // never a blank card: say what went wrong and keep the reason in the browser console
      console.error("nimbus-solar-card:", err);
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `<style>${NSC_CSS}</style><ha-card><div class="card"><h2>Solar Lab</h2>
        <p class="note">The card could not draw this update (${this._esc(err && err.message)}). It tries again with the next one.</p></div></ha-card>`;
      this._key = null; // so the next update draws again
    }
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

    // before the first forecast: how far it has got (it needs about a week of weather history)
    const need = 7;
    const have = +a.history_days || 0;
    const progress = a.status === "fetching_history" || a.status === "learning"
      ? (have >= need ? "learning your panels" : `${have} of ${need} days of weather downloaded`)
      : "";
    this._waiting = progress
      ? (have >= need
          ? `Getting ready: all ${need} days of weather history downloaded; now learning your panels. The first forecast follows in a minute or two.`
          : `Getting ready: ${have} of ${need} days of weather history downloaded. About 3 more days every 6 minutes; first forecast in about ${Math.max(5, Math.ceil(((need - have) / 3) * 6) + 3)} minutes.`)
      : "";

    // header strip
    const made = a.made_at
      ? new Date(a.made_at).toLocaleString(undefined, { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false })
      : "–";
    const runs = a.weather_runs || {};
    const src = ["ecmwf", "gefs", "gfs"].filter((k) => runs[k]).map((k) => `${k.toUpperCase()} ${this._esc(runs[k])}`).join(" · ");
    const live = a.live_correction == null ? "live correction: waiting for sun" : `live correction ×${(+a.live_correction).toFixed(2)}`;
    const top = `<div class="top"><h1>Solar Lab</h1><span>forecast ${this._esc(made)}</span><span>weather: ${src || "–"}</span>
      <span>${this._esc(live)}</span><span>${this._esc(statusText)}${progress ? " · " + this._esc(progress) : ""}${a.status_detail ? " · " + this._esc(a.status_detail) : ""}</span></div>`;

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
      <div class="wbox" id="wbox"><canvas id="cWx"></canvas><div class="wtip" id="wtip" hidden></div></div>
      <div class="leg"><span><i style="background:var(--sun)"></i>forecast (p50)</span><span><i style="background:var(--band);height:9px"></i>p10–p90</span>
      <span><i style="background:var(--clear)"></i>clear sky</span><span><i style="background:var(--act)"></i>measured</span>
      <span>weather every 3 hours, day and night (ECMWF): hover or tap for detail</span></div></div>`;

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
      all three models reach about four days; days after tomorrow are planned on only once its bias at that range is learned.${othersNote}</p></div>`;

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
          summary += `<tr><td>${this._esc(n)}</td><td>0</td><td colspan="4" class="why">no evening-before figures yet: Nimbus notes them itself at 21:00 each evening</td></tr>`;
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
          ? " The other solar forecasts installed here are found automatically and judged on their tomorrow total as it stood at 21:00 the evening before (from the recorder, or noted by Nimbus itself when the recorder does not keep it); yardsticks only, Nimbus never uses them."
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
    // observe the chart boxes themselves (new ones each update) and draw once laid out
    if (this._ro) this.shadowRoot.querySelectorAll(".cbox,.wbox").forEach((b) => this._ro.observe(b));
    this._wxSel = null;
    this._wxSetup();
    this._charts();
    this._redrawSoon();
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
    if (!this._st || !this.shadowRoot) return;
    const { f, m } = this._series();
    this._map = null;
    this._mainChart(f, m);
    this._miniChart(f, m);
    this._wxPrepare(f, m);
    this._wxDraw();
    this._wxAnimate();
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
      x.fillText(this._waiting || "The first forecast comes once it has about a week of history.", L, h / 2);
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
    this._map = { t0, t1, L, now }; // the weather strip below lines up with this chart
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

  // ---- weather strip ------------------------------------------------------------------------
  // Under the main chart, on the same time axis: one picture per 3-hour ECMWF interval (the sky is
  // chosen from cloud, rain, shower rain and storm energy in solar_forecaster/weather.py), night
  // softly shaded, rain as bars, the now line. Gently animated -- the sun glows, rain falls,
  // lightning flashes -- at about 12 frames a second, only while the page is visible, and still when
  // the device asks for reduced motion. Hover or tap a picture for its numbers.
  static get SKY_WORDS() {
    return {
      clear: "clear", mostly_sunny: "mostly sunny", partly_cloudy: "partly cloudy", cloudy: "cloudy",
      sprinkle: "light rain", showers_sun: "sun and showers", rain: "rain", heavy_rain: "heavy rain",
      storm: "thunderstorms", clear_night: "clear night", partly_cloudy_night: "partly cloudy night",
      showers_night: "showers",
    };
  }

  static get SKY_RANK() {
    return {
      clear: 0, clear_night: 0, mostly_sunny: 1, partly_cloudy: 2, partly_cloudy_night: 2, cloudy: 3,
      showers_sun: 4, showers_night: 4, sprinkle: 5, rain: 6, heavy_rain: 7, storm: 8,
    };
  }

  _wxCols() {
    const dark = this.hasAttribute("dark");
    return dark
      ? {
          sun: "#ffb347", sunCore: "#ffe6a3", cloud: ["#eef1f6", "#a4adbd"], back: ["#8a93a3", "#5f6878"],
          wet: ["#c2c9d5", "#768092"], rain: "#5fa8ff", bolt: ["#fff6c2", "#ffc928"], moon: ["#ffffff", "#c9d1de"],
          night: "rgba(120,145,215,.10)", dim: this._var("--dim"), fg: this._var("--fg"),
        }
      : {
          sun: "#e89400", sunCore: "#ffd66b", cloud: ["#c2cad6", "#7f8a9c"], back: ["#d9dee6", "#aab2bf"],
          wet: ["#9aa3b1", "#5f697b"], rain: "#2a7ad6", bolt: ["#ffe37a", "#f0a800"], moon: ["#8fa0c4", "#5a6b92"],
          night: "rgba(55,75,130,.07)", dim: this._var("--dim"), fg: this._var("--fg"),
        };
  }

  _wxSetup() {
    const box = this.shadowRoot && this.shadowRoot.getElementById("wbox");
    if (!box) return;
    const pick = (ev) => {
      const r = box.getBoundingClientRect();
      const px = ev.clientX - r.left;
      const cell = (this._wx ? this._wx.cells : []).find((c) => px >= c.x0 && px < c.x1);
      this._wxSel = cell ? cell.key : null;
      this._wxTip(cell);
      this._wxDraw();
    };
    box.addEventListener("pointermove", pick);
    box.addEventListener("pointerdown", pick);
    box.addEventListener("pointerleave", (ev) => {
      if (ev.pointerType !== "mouse") return; // a tap keeps its detail until the next tap
      this._wxSel = null;
      this._wxTip(null);
      this._wxDraw();
    });
  }

  _wxPrepare(f, m) {
    this._wx = null;
    const c = this._cv("cWx");
    const map = this._map;
    const a = (this._st && this._st.attributes) || {};
    const list = Array.isArray(a.weather) ? a.weather : [];
    if (!c || !map) return;
    const { t0, t1, L, now } = map;
    const { w } = c;
    const X = (s) => L + ((w - L) * (s - t0)) / (t1 - t0);
    const H = 10800;
    const es = list.map((e) => ({ ...e, t: Date.parse(e.time) / 1000 })).filter((e) => e.t + H / 2 > t0 && e.t - H / 2 < t1);
    // night: from the clear-sky forecast where there is one (exact to 15 minutes), else each interval's sun
    const night = [];
    for (let k = 0; k < f.length; k++) {
      const end = k + 1 < f.length ? f[k + 1].t : f[k].t + 900;
      if ((f[k].clear || 0) <= 0.01) night.push([f[k].t, end]);
    }
    const fStart = f.length ? f[0].t : t1;
    for (const e of es) if (!e.sun_up && e.t - H / 2 < fStart) night.push([e.t - H / 2, Math.min(e.t + H / 2, fStart)]);
    // a picture needs about 22 px: on a narrow screen neighbouring intervals share one picture,
    // the worst weather among them (a storm is never hidden behind a sunny neighbour)
    const cellW = ((w - L) * H) / (t1 - t0);
    const n = Math.max(1, Math.ceil(22 / Math.max(cellW, 1)));
    const groups = new Map();
    for (const e of es) {
      const key = Math.floor(e.t / (H * n));
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(e);
    }
    const R = NimbusSolarCard.SKY_RANK;
    const cells = [];
    for (const [key, g] of groups) {
      const a0 = Math.min(...g.map((e) => e.t)) - H / 2;
      const a1 = Math.max(...g.map((e) => e.t)) + H / 2;
      const worst = g.reduce((b, e) => ((R[e.sky] ?? 0) > (R[b.sky] ?? 0) ? e : b), g[0]);
      cells.push({ key, g, a0, a1, x0: Math.max(L, X(a0)), x1: Math.min(w, X(a1)), sky: worst.sky, seed: (Math.abs(key) * 0.618) % 1 });
    }
    this._wx = { c, X, L, now, night, es, cells, f, m, full: cellW * n, cols: this._wxCols() };
  }

  _wxDraw() {
    const W = this._wx;
    if (!W) return;
    const { c, X, L, now, night, es, cells, cols } = W;
    const { x, w, h } = c;
    const still = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    const p = still ? 0 : performance.now() / 1000;
    x.clearRect(0, 0, w, h);
    const iconH = 48;
    for (const [a0, a1] of night) {
      const xa = Math.max(L, X(a0));
      const xb = Math.min(w, X(a1));
      if (xb > xa) {
        x.fillStyle = cols.night;
        x.fillRect(xa, 0, xb - xa + 0.5, h);
      }
    }
    for (const cell of cells) {
      if (cell.key !== this._wxSel) continue;
      x.fillStyle = cols.fg;
      x.globalAlpha = 0.07;
      this._round(x, cell.x0 + 1, 1, cell.x1 - cell.x0 - 2, h - 2, 6);
      x.fill();
      x.globalAlpha = 1;
    }
    // rain bars per interval, rounded: full height at 5 mm/h (square root, so light rain still shows)
    const base = h - 1;
    const room = h - iconH - 5;
    for (const e of es) {
      if (!(e.rain > 0.02)) continue;
      const xa = Math.max(L, X(e.t - 5400)) + 1.5;
      const xb = Math.min(w, X(e.t + 5400)) - 1.5;
      if (xb <= xa) continue;
      const bh = Math.max(2, room * Math.min(1, Math.sqrt(e.rain / 5)));
      const g = x.createLinearGradient(0, base - room, 0, base);
      g.addColorStop(0, cols.rain);
      g.addColorStop(1, cols.rain + "55");
      x.fillStyle = g;
      this._round(x, xa, base - bh, xb - xa, bh, Math.min(3, (xb - xa) / 2));
      x.fill();
    }
    const full = W.full;
    for (const cell of cells) {
      const cw = cell.x1 - cell.x0;
      if (cw < Math.max(8, 0.6 * full)) continue; // a picture cut by the chart's edge is left out
      const s = Math.max(14, Math.min(cw - 2, 40));
      this._sky(x, cell.sky, (cell.x0 + cell.x1) / 2, iconH / 2 + 1, s, p, cell.seed, cols, still);
    }
    x.fillStyle = cols.dim;
    x.font = "10px ui-monospace, Consolas, monospace";
    x.fillText("sky", 2, iconH / 2 + 4);
    x.fillText("rain", 2, h - 3);
    const Xn = X(now);
    x.strokeStyle = cols.fg;
    x.globalAlpha = 0.4;
    x.beginPath();
    x.moveTo(Xn, 0);
    x.lineTo(Xn, h);
    x.stroke();
    x.globalAlpha = 1;
  }

  _wxAnimate() {
    if (this._wxTimer || !this.isConnected) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    this._wxTimer = setInterval(() => {
      if (!this.isConnected) {
        clearInterval(this._wxTimer);
        this._wxTimer = null;
        return;
      }
      if (document.visibilityState === "visible" && this._wx) this._wxDraw();
    }, 83);
  }

  _wxTip(cell) {
    const tip = this.shadowRoot && this.shadowRoot.getElementById("wtip");
    if (!tip) return;
    if (!cell || !this._wx) {
      tip.hidden = true;
      return;
    }
    const tz = this._tz();
    const fmt = (s, wd) => new Date(s * 1000).toLocaleString(undefined, { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false, ...(wd ? { weekday: "short" } : {}) });
    const g = cell.g;
    const vals = (k) => g.map((e) => e[k]).filter((v) => v != null);
    const mx = (k) => (vals(k).length ? Math.max(...vals(k)) : null);
    const mean = (k) => (vals(k).length ? vals(k).reduce((s, v) => s + v, 0) / vals(k).length : null);
    const avg = (v) => (v.length ? v.reduce((s, v2) => s + v2, 0) / v.length : null);
    const { f, m } = this._wx;
    const inSpan = (r) => r.t >= cell.a0 && r.t < cell.a1;
    const lines = [`<b>${this._esc(fmt(cell.a0, true))}–${this._esc(fmt(cell.a1))}</b> · ${this._esc(NimbusSolarCard.SKY_WORDS[cell.sky] || cell.sky)}`];
    const tm = vals("temperature");
    if (tm.length) lines[0] += ` · ${Math.round(Math.min(...tm))}${Math.max(...tm) - Math.min(...tm) >= 1 ? "–" + Math.round(Math.max(...tm)) : ""} °C`;
    const cl = mean("cloud");
    const lc = mean("low_cloud");
    if (cl != null) lines.push(`cloud ${Math.round(cl)} %${lc != null ? ` (low ${Math.round(lc)} %)` : ""}`);
    const rn = mx("rain");
    const sh = mx("shower");
    lines.push(rn != null ? `rain ${rn.toFixed(1)} mm/h${sh != null && sh >= 0.05 ? ` (showers ${sh.toFixed(1)})` : ""}` : "rain –");
    const cp = mx("cape");
    if (cp != null && cp >= 300) lines.push(`storm energy ${Math.round(cp)} J/kg${cp >= 800 ? " (thunderstorms possible)" : ""}`);
    const ma = avg(m.filter(inSpan).map((r) => r.v));
    const fa = avg(f.filter(inSpan).map((r) => r.value));
    if (ma != null) lines.push(`measured ${ma.toFixed(1)} kW`);
    if (fa != null && fa >= 0.05) lines.push(`solar forecast ${fa.toFixed(1)} kW`);
    tip.innerHTML = lines.join("<br>");
    tip.hidden = false;
    const bw = this._wx.c.w;
    const half = (tip.offsetWidth || 160) / 2;
    tip.style.left = `${Math.min(Math.max((cell.x0 + cell.x1) / 2, half + 2), bw - half - 2)}px`;
  }

  _round(x, X0, Y0, w, h, r) {
    x.beginPath();
    if (x.roundRect) x.roundRect(X0, Y0, w, h, [r, r, 0, 0]);
    else x.rect(X0, Y0, w, h);
  }

  // one sky picture, s px square, centred on (cx, cy); p = seconds (0 when still)
  _sky(x, sky, cx, cy, s, p, seed, C, still) {
    const night = sky.endsWith("_night");
    const body = (dx, dy, r) => (night ? this._moon(x, cx + dx, cy + dy, r, C) : this._sun(x, cx + dx, cy + dy, r, p, C));
    switch (sky) {
      case "clear":
      case "clear_night":
        body(0, 0, s * 0.24);
        break;
      case "mostly_sunny":
        body(-s * 0.08, -s * 0.08, s * 0.22);
        this._cloud(x, cx + s * 0.16, cy + s * 0.12, s * 0.56, C.cloud);
        break;
      case "partly_cloudy":
      case "partly_cloudy_night":
        body(-s * 0.15, -s * 0.14, s * 0.2);
        this._cloud(x, cx + s * 0.06, cy + s * 0.05, s * 0.82, C.cloud);
        break;
      case "cloudy":
        this._cloud(x, cx + s * 0.14, cy - s * 0.12, s * 0.62, C.back);
        this._cloud(x, cx - s * 0.04, cy + s * 0.03, s * 0.86, C.cloud);
        break;
      case "showers_sun":
      case "showers_night":
        body(-s * 0.17, -s * 0.2, s * 0.18);
        this._cloud(x, cx + s * 0.04, cy - s * 0.1, s * 0.8, C.cloud);
        this._drops(x, cx + s * 0.04, cy + s * 0.13, s, 2, false, p, seed, C);
        break;
      case "sprinkle":
        this._cloud(x, cx, cy - s * 0.12, s * 0.86, C.cloud);
        this._dots(x, cx, cy + s * 0.15, s, p, seed, C);
        break;
      case "rain":
        this._cloud(x, cx, cy - s * 0.12, s * 0.88, C.wet);
        this._drops(x, cx, cy + s * 0.13, s, 3, false, p, seed, C);
        break;
      case "heavy_rain":
        this._cloud(x, cx, cy - s * 0.12, s * 0.9, C.wet);
        this._drops(x, cx, cy + s * 0.13, s, 4, true, p, seed, C);
        break;
      case "storm": {
        // a quick double flash every few seconds, each picture on its own beat
        const q = still ? 0.05 : (p + seed * 2.3) % 2.8;
        const on = q < 0.1 || (q > 0.2 && q < 0.27);
        this._cloud(x, cx, cy - s * 0.12, s * 0.9, on ? C.cloud : C.wet);
        this._drops(x, cx - s * 0.18, cy + s * 0.13, s, 2, false, p, seed, C);
        this._bolt(x, cx + s * 0.08, cy + s * 0.06, s * 1.3, on, C);
        break;
      }
      default:
        this._cloud(x, cx, cy, s * 0.86, C.cloud);
    }
  }

  _sun(x, cx, cy, r, p, C) {
    const glow = 0.5 + 0.5 * Math.sin((p * 2 * Math.PI) / 3.2); // a slow, soft breathing glow
    const R = r * (2 + 0.3 * glow);
    const halo = x.createRadialGradient(cx, cy, r * 0.8, cx, cy, R);
    halo.addColorStop(0, C.sun + "66");
    halo.addColorStop(1, C.sun + "00");
    x.globalAlpha = 0.55 + 0.45 * glow;
    x.fillStyle = halo;
    x.beginPath();
    x.arc(cx, cy, R, 0, 2 * Math.PI);
    x.fill();
    x.globalAlpha = 1;
    x.strokeStyle = C.sun;
    x.lineWidth = Math.max(1.3, r * 0.17);
    x.lineCap = "round";
    const turn = p * 0.12;
    for (let k = 0; k < 8; k++) {
      const an = (k * Math.PI) / 4 + turn;
      const r2 = r * (1.66 + 0.1 * glow);
      x.beginPath();
      x.moveTo(cx + Math.cos(an) * r * 1.38, cy + Math.sin(an) * r * 1.38);
      x.lineTo(cx + Math.cos(an) * r2, cy + Math.sin(an) * r2);
      x.stroke();
    }
    const core = x.createRadialGradient(cx - r * 0.3, cy - r * 0.3, r * 0.1, cx, cy, r);
    core.addColorStop(0, C.sunCore);
    core.addColorStop(1, C.sun);
    x.fillStyle = core;
    x.beginPath();
    x.arc(cx, cy, r, 0, 2 * Math.PI);
    x.fill();
  }

  _moon(x, cx, cy, r, C) {
    const halo = x.createRadialGradient(cx, cy, r * 0.6, cx, cy, r * 1.9);
    halo.addColorStop(0, C.moon[1] + "40");
    halo.addColorStop(1, C.moon[1] + "00");
    x.fillStyle = halo;
    x.beginPath();
    x.arc(cx, cy, r * 1.9, 0, 2 * Math.PI);
    x.fill();
    const g = x.createLinearGradient(cx - r, cy - r, cx + r, cy + r);
    g.addColorStop(0, C.moon[0]);
    g.addColorStop(1, C.moon[1]);
    x.fillStyle = g;
    x.beginPath();
    x.arc(cx, cy, r, Math.PI / 2, (3 * Math.PI) / 2);
    x.ellipse(cx, cy, r * 0.42, r, 0, (3 * Math.PI) / 2, Math.PI / 2, true);
    x.fill();
  }

  _cloud(x, cx, cy, sw, col) {
    const g = x.createLinearGradient(0, cy - 0.37 * sw, 0, cy + 0.27 * sw); // lit from above
    g.addColorStop(0, col[0]);
    g.addColorStop(1, col[1]);
    x.fillStyle = g;
    x.beginPath();
    for (const [dx, dy, r] of [[-0.24, 0.07, 0.19], [0.02, -0.09, 0.27], [0.26, 0.07, 0.18]]) {
      x.moveTo(cx + dx * sw + r * sw, cy + dy * sw);
      x.arc(cx + dx * sw, cy + dy * sw, r * sw, 0, 2 * Math.PI);
    }
    if (x.roundRect) x.roundRect(cx - 0.43 * sw, cy + 0.02 * sw, 0.87 * sw, 0.24 * sw, 0.12 * sw);
    else x.rect(cx - 0.43 * sw, cy + 0.02 * sw, 0.87 * sw, 0.24 * sw);
    x.fill();
  }

  _drops(x, cx, top, s, n, heavy, p, seed, C) {
    x.strokeStyle = C.rain;
    x.lineWidth = heavy ? Math.max(1.7, s * 0.07) : Math.max(1.3, s * 0.055);
    x.lineCap = "round";
    for (let i = 0; i < n; i++) {
      const dx = (i - (n - 1) / 2) * s * 0.19;
      const fall = (p * 1.3 + i * 0.37 + seed) % 1; // falling, each drop on its own beat
      const y0 = top + fall * s * 0.15;
      x.globalAlpha = (1 - fall * 0.7) * (0.75 + 0.25 * Math.sin(p * 9 + i * 2.1 + seed * 7)); // fades as it falls, flickers
      x.beginPath();
      x.moveTo(cx + dx, y0);
      x.lineTo(cx + dx - s * 0.045, y0 + s * (heavy ? 0.16 : 0.12));
      x.stroke();
    }
    x.globalAlpha = 1;
  }

  _dots(x, cx, top, s, p, seed, C) {
    x.fillStyle = C.rain;
    for (let i = 0; i < 3; i++) {
      const dx = (i - 1) * s * 0.19;
      x.globalAlpha = 0.35 + 0.65 * Math.abs(Math.sin(p * 2.2 + i * 1.1 + seed * 5)); // twinkle
      x.beginPath();
      x.arc(cx + dx, top + (i % 2) * s * 0.09 + s * 0.03, Math.max(1.1, s * 0.045), 0, 2 * Math.PI);
      x.fill();
    }
    x.globalAlpha = 1;
  }

  _bolt(x, cx, top, s, on, C) {
    if (on) {
      const flash = x.createRadialGradient(cx, top + s * 0.18, 0, cx, top + s * 0.18, s * 0.45);
      flash.addColorStop(0, C.bolt[1] + "70");
      flash.addColorStop(1, C.bolt[1] + "00");
      x.fillStyle = flash;
      x.beginPath();
      x.arc(cx, top + s * 0.18, s * 0.45, 0, 2 * Math.PI);
      x.fill();
    }
    const g = x.createLinearGradient(0, top, 0, top + s * 0.38);
    g.addColorStop(0, C.bolt[0]);
    g.addColorStop(1, C.bolt[1]);
    x.fillStyle = g;
    x.globalAlpha = on ? 1 : 0.6;
    x.beginPath();
    x.moveTo(cx + s * 0.03, top);
    x.lineTo(cx - s * 0.12, top + s * 0.2);
    x.lineTo(cx - s * 0.01, top + s * 0.2);
    x.lineTo(cx - s * 0.08, top + s * 0.38);
    x.lineTo(cx + s * 0.13, top + s * 0.14);
    x.lineTo(cx + s * 0.02, top + s * 0.14);
    x.lineTo(cx + s * 0.09, top);
    x.closePath();
    x.fill();
    x.globalAlpha = 1;
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
