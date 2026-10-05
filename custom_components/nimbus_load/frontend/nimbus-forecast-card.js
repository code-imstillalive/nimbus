// nimbus-forecast-card.js
//
// nimbus issue #1529: the Forecaster shipped no chart. The three cards
// already registered by frontend.py are all Solver-side, so a household
// that followed the setup guide could see its forecasts only as a sensor
// whose state is one number and whose `forecast` attribute is a list.
// Reported by a tester (#1526): "Forecaster doesn't provide apex chart at
// all ... no way of telling how the forecaster works."
//
// This card needs no configuration and no HACS card dependency. It reads
// only what every Nimbus forecast sensor already publishes:
//
//   forecast            [{time, value, lower?, upper?}, ...]
//   source_sensor       the measured entity the model learns from; its
//                       recorder history is drawn as the "measured" line
//   validation_*_mae    per-candidate error, used for the model panel
//   model_trained_at / training_points / training_span_days
//
// Which model is in use mirrors ml/model.py's own selection rule exactly
// (search "model_type = min(recursive_mae"): the lowest RECURSIVE
// validation MAE when every candidate has one, otherwise the lowest
// one-step MAE, otherwise the k-NN default for a load with too little
// validation data. `naive` is a real candidate there, so the card says
// plainly when persistence beats the trained models instead of implying
// the ML always wins.
//
// Same plain-HTMLElement / shadow-DOM / inline-SVG style as the other
// shipped cards, for consistency and so nothing extra has to be installed.

const NIMBUS_FC_SVG_NS = "http://www.w3.org/2000/svg";

class NimbusForecastCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    this._historyHours = Number(this._config.history_hours) > 0 ? Number(this._config.history_hours) : 24;
    this._forecastHours = Number(this._config.forecast_hours) > 0 ? Number(this._config.forecast_hours) : 48;
    this._selected = this._config.entity || null;
    this._history = {}; // entity_id -> {fetchedAt, points: [[ms, value]]}
    this._lastRenderKey = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
    const entities = this._discover();
    if (!this._selected || !hass.states[this._selected]) {
      this._selected = entities.length ? entities[0].entity_id : null;
    }
    const sel = this._selected ? hass.states[this._selected] : null;
    if (sel) this._maybeFetchHistory(sel);
    // Re-render only when something we draw actually changed: the entity
    // list, the selected sensor's own update, its history, or a minute
    // boundary (so the "now" marker moves). hass is set many times a
    // second on a busy install.
    const hist = sel ? this._history[sel.entity_id] : null;
    const key = [
      entities.map((e) => e.entity_id).join(","),
      this._selected,
      sel ? sel.last_updated : "",
      hist ? hist.fetchedAt : "",
      Math.floor(Date.now() / 60000),
    ].join("|");
    if (key !== this._lastRenderKey) {
      this._lastRenderKey = key;
      this._render(entities);
    }
  }

  getCardSize() {
    return 8;
  }

  // Every Nimbus Load / Power Signal forecast, plus the whole-house rollup.
  // Discovered from live state, never a hardcoded list. The Solver's own
  // published plan (sensor.nimbus_solver_*) is excluded: it is a dispatch
  // plan, not a Forecaster output, and has its own card.
  _discover() {
    if (Array.isArray(this._config.entities) && this._config.entities.length) {
      return this._config.entities
        .map((id) => this._hass.states[id])
        .filter((s) => s && Array.isArray(s.attributes.forecast));
    }
    const out = [];
    for (const [id, s] of Object.entries(this._hass.states)) {
      if (!id.startsWith("sensor.nimbus_") || !id.endsWith("_forecast")) continue;
      if (id.startsWith("sensor.nimbus_solver_")) continue;
      const fc = s.attributes && s.attributes.forecast;
      if (!Array.isArray(fc)) continue;
      out.push(s);
    }
    // First: the whole-house forecast the Solver is actually configured to
    // plan with (its own "load forecast sensor"), so the card opens on the
    // number that drives dispatch. Then the other Power Signals, then the
    // summed-circuits rollup, then each Load (circuit).
    const cfgState = this._hass.states["sensor.nimbus_solver_config"];
    const solverLoad = cfgState && cfgState.attributes.solver_load_forecast_sensor;
    const rank = (s) => {
      if (solverLoad && s.entity_id === solverLoad) return 0;
      if (s.attributes.subentry_type === "power_signal") return 1;
      if (s.entity_id === "sensor.nimbus_household_load_total_forecast") return 2;
      if (s.attributes.subentry_type === "load") return 3;
      return 4;
    };
    out.sort((a, b) => rank(a) - rank(b) || this._label(a).localeCompare(this._label(b)));
    return out;
  }

  _label(s) {
    const name = (s.attributes.friendly_name || s.entity_id).replace(/\s*forecast$/i, "");
    return name.trim();
  }

  // The measured sensors behind a forecast. A Load or Power Signal names
  // one (`source_sensor`). The whole-house rollup has none of its own: it
  // is a sum of circuit forecasts (`source_entities`), so its measured
  // line is the same sum of those circuits' own source sensors.
  _measuredSources(sel) {
    const a = sel.attributes;
    if (a.source_sensor) return [{ id: a.source_sensor, scale: this._unitScale(sel, a.source_sensor) }];
    if (!Array.isArray(a.source_entities)) return [];
    const out = [];
    for (const fe of a.source_entities) {
      const f = this._hass.states[fe];
      const src = f && f.attributes.source_sensor;
      if (src) out.push({ id: src, scale: this._unitScale(f, src) });
    }
    return out;
  }

  _maybeFetchHistory(sel) {
    const sources = this._measuredSources(sel);
    if (!sources.length) return;
    const cached = this._history[sel.entity_id];
    const now = Date.now();
    if (cached && (cached.pending || now - cached.fetchedAt < 5 * 60 * 1000)) return;
    this._history[sel.entity_id] = { fetchedAt: cached ? cached.fetchedAt : 0, points: cached ? cached.points : [], pending: true };
    const start = new Date(now - this._historyHours * 3600 * 1000).toISOString();
    const ids = sources.map((s) => s.id).join(",");
    this._hass
      .callApi("GET", "history/period/" + start + "?filter_entity_id=" + ids + "&minimal_response&no_attributes")
      .then((data) => {
        const byId = {};
        for (const series of data || []) {
          if (!series.length) continue;
          const id = series[0].entity_id;
          const src = sources.find((s) => s.id === id);
          if (!src) continue;
          byId[id] = series
            .map((p) => [new Date(p.last_changed || p.lu * 1000).getTime(), parseFloat(p.state) * src.scale])
            .filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
        }
        const lists = Object.values(byId);
        const points = lists.length === 1 ? lists[0] : this._sumSeries(lists, now - this._historyHours * 3600 * 1000, now);
        this._history[sel.entity_id] = { fetchedAt: Date.now(), points, summed: sources.length > 1 ? lists.length : 0 };
        this._lastRenderKey = null;
        if (this._hass) this.hass = this._hass;
      })
      .catch(() => {
        this._history[sel.entity_id] = { fetchedAt: Date.now(), points: [], error: true };
      });
  }

  // Sum several recorder series on a 5-minute grid, each held at its last
  // recorded value (HA records on change, so a held value is real, not a
  // gap). A step where no series has started yet is left out.
  _sumSeries(lists, t0, t1) {
    const step = 5 * 60 * 1000;
    const idx = lists.map(() => 0);
    const out = [];
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      let sum = 0;
      let any = false;
      lists.forEach((l, i) => {
        while (idx[i] + 1 < l.length && l[idx[i] + 1][0] <= t) idx[i]++;
        if (l.length && l[idx[i]][0] <= t) {
          sum += l[idx[i]][1];
          any = true;
        }
      });
      if (any) out.push([t, sum]);
    }
    return out;
  }

  // The forecast is in the forecast sensor's unit; the measured source may
  // be in W while the forecast is in kW (the coordinator converts power
  // sources to kW before training). Scale the measured line to match.
  _unitScale(sel, src) {
    const srcState = this._hass.states[src];
    const fu = (sel.attributes.unit_of_measurement || "").toLowerCase();
    const su = ((srcState && srcState.attributes.unit_of_measurement) || "").toLowerCase();
    if (fu === "kw" && su === "w") return 0.001;
    if (fu === "kw" && su === "mw") return 1000;
    if (fu === "w" && su === "kw") return 1000;
    return 1;
  }

  // Mirrors ml/model.py's selection rule; see the header comment.
  _modelInfo(attrs) {
    const rec = attrs.validation_recursive_mae || {};
    const one = attrs.validation_mae || {};
    const names = { knn: "k-NN", gbrt: "Gradient boosting", naive: "Seasonal persistence" };
    const finite = (d) => Object.entries(d).filter(([, v]) => Number.isFinite(v));
    const oneKeys = finite(one);
    const recKeys = finite(rec);
    let basis = null;
    let table = null;
    if (oneKeys.length && recKeys.length === oneKeys.length) {
      basis = "recursive";
      table = recKeys;
    } else if (oneKeys.length) {
      basis = "one-step";
      table = oneKeys;
    }
    if (!table) {
      return { chosen: "knn", label: names.knn, basis: null, skill: null, naiveWins: false };
    }
    table.sort((a, b) => a[1] - b[1]);
    const chosen = table[0][0];
    const naive = table.find(([k]) => k === "naive");
    let skill = null;
    if (naive && naive[1] > 0 && chosen !== "naive") skill = 1 - table[0][1] / naive[1];
    return {
      chosen, label: names[chosen] || chosen, basis, skill, naiveWins: chosen === "naive",
      err: table[0][1], naiveErr: naive ? naive[1] : null,
    };
  }

  _fmtTime(ms, withDay) {
    const d = new Date(ms);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    if (!withDay) return hh + ":" + mm;
    return d.toLocaleDateString(undefined, { weekday: "short" }) + " " + hh + ":" + mm;
  }

  _fmtNum(v, unit) {
    if (!Number.isFinite(v)) return "—";
    const abs = Math.abs(v);
    const dp = abs >= 100 ? 0 : abs >= 10 ? 1 : 2;
    return v.toFixed(dp) + (unit ? " " + unit : "");
  }

  _render(entities) {
    const root = this.shadowRoot;
    const title = this._config.title || "Nimbus Forecaster";
    if (!entities.length) {
      root.innerHTML =
        this._style() +
        '<ha-card><div class="hdr"><div class="title">' + this._esc(title) + "</div></div>" +
        '<div class="empty">No Nimbus forecasts yet. Add a Load or Power Signal under ' +
        "Settings → Devices &amp; Services → Nimbus. Each one appears here after its first training run.</div></ha-card>";
      return;
    }
    const sel = this._hass.states[this._selected];
    const attrs = sel.attributes;
    const unit = attrs.unit_of_measurement || "";
    const now = Date.now();
    const t0 = now - this._historyHours * 3600 * 1000;
    const t1 = now + this._forecastHours * 3600 * 1000;

    let fc = (attrs.forecast || [])
      .map((p) => ({
        t: new Date(p.time || p.datetime).getTime(),
        v: parseFloat(p.value),
        lo: parseFloat(p.lower),
        hi: parseFloat(p.upper),
      }))
      .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v) && p.t >= now - 3600 * 1000 && p.t <= t1)
      .sort((a, b) => a.t - b.t);
    const hist = (this._history[sel.entity_id] && this._history[sel.entity_id].points) || [];
    const histIn = hist.filter((p) => p[0] >= t0 && p[0] <= now);
    // A signal that never goes negative (a load, a whole-house total) can
    // still publish a negative lower bound; showing the range below zero
    // would tell the household something physically impossible. Applied
    // once here so the chart and the table always agree. Signed signals
    // (battery, grid) keep their full range.
    const nonNeg = histIn.every((p) => p[1] >= 0) && fc.every((p) => p.v >= 0);
    if (nonNeg) fc = fc.map((p) => (Number.isFinite(p.lo) && p.lo < 0 ? { ...p, lo: 0 } : p));

    const chips = entities
      .map((e) => {
        const on = e.entity_id === this._selected;
        return '<button class="chip' + (on ? " on" : "") + '" data-eid="' + this._esc(e.entity_id) + '">' + this._esc(this._label(e)) + "</button>";
      })
      .join("");

    const m = this._modelInfo(attrs);
    const isSum = !attrs.source_sensor && Array.isArray(attrs.source_entities);
    let verdict;
    if (isSum) {
      verdict = "The sum of " + attrs.source_entities.length + " circuit forecasts. Each circuit has its own model; select one above to see how it is doing.";
    } else if (!m.basis) {
      verdict = "Not enough validation data yet to compare models. Using k-NN by default.";
    } else if (m.naiveWins) {
      verdict = "Seasonal persistence (the same time on recent days) currently beats the trained models on this signal, so Nimbus uses it.";
    } else if (m.skill !== null) {
      const pct = Math.round(m.skill * 100);
      verdict = pct >= 0
        ? this._esc(m.label) + " is " + pct + "% more accurate than persistence."
        : this._esc(m.label) + " is in use; persistence is " + -pct + "% more accurate on this metric.";
    } else {
      verdict = this._esc(m.label) + " is in use.";
    }
    const trained = attrs.model_trained_at ? this._fmtTime(new Date(attrs.model_trained_at).getTime(), true) : "never";
    const facts = (isSum
      ? [
          ["Built from", attrs.source_entities.length + " circuits"],
          ["Updated", attrs.generated_at ? this._fmtTime(new Date(attrs.generated_at).getTime(), true) : "—"],
        ]
      : [
      ["Model", this._esc(m.label)],
      ["Compared on", m.basis ? m.basis + " validation" : "—"],
      // The raw errors behind the percentage, so a claim can be judged
      // rather than taken on trust.
      ["Average error", m.basis
        ? this._fmtNum(m.err, unit) + (m.naiveErr !== null && !m.naiveWins ? " vs " + this._fmtNum(m.naiveErr, unit) + " persistence" : "")
        : "—"],
      ["Training data", Number.isFinite(attrs.training_span_days) && attrs.training_span_days > 0
        ? attrs.training_span_days.toFixed(1) + " days · " + (attrs.training_points || 0) + " points"
        : (attrs.training_points || 0) + " points"],
      ["Last trained", trained],
    ])
      .map(([k, v]) => '<div class="fact"><span class="k">' + k + '</span><span class="v">' + v + "</span></div>")
      .join("");

    const nowVal = parseFloat(sel.state);
    root.innerHTML =
      this._style() +
      '<ha-card><div class="hdr"><div class="title">' + this._esc(title) + "</div>" +
      '<div class="now"><span class="nl">' + this._esc(this._label(sel)) + '</span><span class="nv">' + this._fmtNum(nowVal, unit) + "</span></div></div>" +
      '<div class="chips">' + chips + "</div>" +
      '<div class="chart"></div>' +
      '<div class="legend"><span><i class="sw meas"></i>Measured</span><span><i class="sw fc"></i>Forecast</span>' +
      '<span><i class="sw band"></i>Forecast range</span></div>' +
      '<div class="model"><div class="verdict">' + verdict + '</div><div class="facts">' + facts + "</div></div>" +
      this._table(fc, now, unit) +
      "</ha-card>";

    root.querySelectorAll(".chip").forEach((b) =>
      b.addEventListener("click", () => {
        this._selected = b.dataset.eid;
        this._lastRenderKey = null;
        this.hass = this._hass;
      })
    );
    root.querySelector(".chart").appendChild(this._chart(histIn, fc, t0, t1, now, unit));
    const det = root.querySelector("details.tbl");
    if (det) det.addEventListener("toggle", () => { this._tableOpen = det.open; });
  }

  // The forecast as an hourly table, for reading exact values rather than
  // judging them off the chart. Each hour shows the forecast point in force
  // at that moment (the forecast is a step series, held until the next
  // point), so the table and the chart can never disagree.
  _table(fc, now, unit) {
    if (!fc.length) return "";
    const hours = Math.min(24, this._forecastHours);
    const first = Math.ceil(now / 3600000) * 3600000;
    const rows = [];
    let i = 0;
    for (let k = 0; k < hours; k++) {
      const t = first + k * 3600000;
      while (i + 1 < fc.length && fc[i + 1].t <= t) i++;
      if (fc[i].t > t) continue;
      const p = fc[i];
      const range = Number.isFinite(p.lo) && Number.isFinite(p.hi)
        ? this._fmtNum(p.lo, "") + " – " + this._fmtNum(p.hi, "")
        : "—";
      rows.push(
        "<tr><td>" + this._fmtTime(t, new Date(t).getHours() === 0 || k === 0) + '</td><td class="n">' +
        this._fmtNum(p.v, "") + '</td><td class="n r">' + range + "</td></tr>"
      );
    }
    if (!rows.length) return "";
    const u = unit ? " (" + this._esc(unit) + ")" : "";
    return (
      '<details class="tbl"' + (this._tableOpen ? " open" : "") + "><summary>Hourly forecast, next " + hours + " h</summary>" +
      "<table><thead><tr><th>Time</th><th class=\"n\">Forecast" + u + '</th><th class="n">Range' + u + "</th></tr></thead><tbody>" +
      rows.join("") + "</tbody></table></details>"
    );
  }

  _chart(hist, fc, t0, t1, now, unit) {
    const W = 640, H = 240, padL = 46, padR = 12, padT = 18, padB = 26;
    const pw = W - padL - padR, ph = H - padT - padB;
    const vals = [];
    hist.forEach((p) => vals.push(p[1]));
    fc.forEach((p) => {
      vals.push(p.v);
      if (Number.isFinite(p.lo)) vals.push(p.lo);
      if (Number.isFinite(p.hi)) vals.push(p.hi);
    });
    let lo = vals.length ? Math.min(...vals) : 0;
    let hi = vals.length ? Math.max(...vals) : 1;
    if (lo > 0) lo = 0;
    if (hi - lo < 1e-9) hi = lo + 1;
    // Round axis: a 1/2/5 x 10^n step giving about four intervals.
    const raw = (hi - lo) / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const tick = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
    lo = Math.floor(lo / tick) * tick;
    hi = Math.ceil(hi / tick) * tick;
    const x = (t) => padL + ((t - t0) / (t1 - t0)) * pw;
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * ph;

    const svg = document.createElementNS(NIMBUS_FC_SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("width", "100%");
    svg.setAttribute("role", "img");
    const add = (tag, attrs, text) => {
      const el = document.createElementNS(NIMBUS_FC_SVG_NS, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
      if (text !== undefined) el.textContent = text;
      svg.appendChild(el);
      return el;
    };

    // y grid + labels
    const dp = tick >= 1 ? 0 : tick >= 0.1 ? 1 : 2;
    for (let v = lo; v <= hi + tick / 2; v += tick) {
      const yy = y(v);
      add("line", { x1: padL, x2: W - padR, y1: yy, y2: yy, class: v === 0 && lo < 0 ? "grid zero" : "grid" });
      add("text", { x: padL - 6, y: yy + 3, class: "ax", "text-anchor": "end" }, (Math.abs(v) < tick / 1e6 ? 0 : v).toFixed(dp));
    }
    if (unit) add("text", { x: padL - 6, y: padT - 7, class: "ax", "text-anchor": "end" }, unit);
    // x labels every 6 h on the hour
    const first = Math.ceil(t0 / 3600000) * 3600000;
    for (let t = first; t <= t1; t += 3600000) {
      const d = new Date(t);
      if (d.getHours() % 6 !== 0) continue;
      const xx = x(t);
      add("line", { x1: xx, x2: xx, y1: padT, y2: H - padB, class: "grid v" });
      add("text", { x: xx, y: H - 8, class: "ax", "text-anchor": "middle" },
        d.getHours() === 0 ? d.toLocaleDateString(undefined, { weekday: "short" }) : String(d.getHours()).padStart(2, "0") + ":00");
    }

    // forecast range band
    const band = fc.filter((p) => Number.isFinite(p.lo) && Number.isFinite(p.hi));
    if (band.length > 1) {
      const top = band.map((p) => x(p.t) + "," + y(p.hi)).join(" ");
      const bot = band.slice().reverse().map((p) => x(p.t) + "," + y(p.lo)).join(" ");
      add("polygon", { points: top + " " + bot, class: "band" });
    }
    // measured history (step-after, as recorded)
    if (hist.length) {
      let d = "";
      hist.forEach((p, i) => {
        const xx = x(p[0]), yy = y(p[1]);
        if (i === 0) d += "M" + xx + "," + yy;
        else d += " H" + xx + " V" + yy;
      });
      d += " H" + x(now);
      add("path", { d, class: "meas" });
    }
    // forecast line
    if (fc.length > 1) {
      add("polyline", { points: fc.map((p) => x(p.t) + "," + y(p.v)).join(" "), class: "fc" });
    }
    // now marker
    add("line", { x1: x(now), x2: x(now), y1: padT, y2: H - padB, class: "nowl" });
    add("text", { x: x(now) + 4, y: padT + 9, class: "ax nowt" }, "now");
    if (!hist.length && !fc.length) {
      add("text", { x: W / 2, y: H / 2, class: "ax", "text-anchor": "middle" }, "No data yet");
    }
    return svg;
  }

  _esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  _style() {
    return (
      "<style>" +
      ":host{display:block}" +
      "ha-card{padding:14px 16px 12px}" +
      ".hdr{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}" +
      ".title{font-size:16px;font-weight:600;color:var(--primary-text-color)}" +
      ".now{display:flex;gap:8px;align-items:baseline}" +
      ".nl{font-size:12px;color:var(--secondary-text-color)}" +
      ".nv{font-size:22px;font-weight:600;color:var(--primary-text-color);font-variant-numeric:tabular-nums}" +
      ".chips{display:flex;gap:6px;overflow-x:auto;padding:10px 0 6px;scrollbar-width:thin}" +
      ".chip{flex:0 0 auto;border:1px solid var(--divider-color,#444);background:transparent;color:var(--secondary-text-color);" +
      "border-radius:14px;padding:4px 10px;font-size:12px;cursor:pointer;white-space:nowrap}" +
      ".chip.on{background:var(--primary-color);border-color:var(--primary-color);color:var(--text-primary-color,#fff)}" +
      ".chart svg{display:block;overflow:visible}" +
      ".grid{stroke:var(--divider-color,#444);stroke-width:1;opacity:.5}" +
      ".grid.v{opacity:.25}" +
      ".grid.zero{opacity:.9}" +
      ".ax{fill:var(--secondary-text-color);font-size:10px;font-family:inherit}" +
      ".band{fill:var(--primary-color);opacity:.16;stroke:none}" +
      ".fc{fill:none;stroke:var(--primary-color);stroke-width:2;stroke-linejoin:round}" +
      ".meas{fill:none;stroke:var(--primary-text-color);stroke-width:1.5;opacity:.85}" +
      ".nowl{stroke:var(--accent-color,#ff9800);stroke-width:1;stroke-dasharray:3 3}" +
      ".nowt{fill:var(--accent-color,#ff9800)}" +
      ".legend{display:flex;gap:14px;font-size:11px;color:var(--secondary-text-color);padding:4px 0 2px;flex-wrap:wrap}" +
      ".sw{display:inline-block;width:14px;height:3px;margin-right:5px;vertical-align:middle;border-radius:2px}" +
      ".sw.meas{background:var(--primary-text-color)}" +
      ".sw.fc{background:var(--primary-color)}" +
      ".sw.band{background:var(--primary-color);opacity:.3;height:8px}" +
      ".model{margin-top:10px;border-top:1px solid var(--divider-color,#444);padding-top:10px}" +
      ".verdict{font-size:13px;color:var(--primary-text-color);margin-bottom:8px}" +
      ".facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:6px 16px}" +
      ".fact{display:flex;flex-direction:column}" +
      ".k{font-size:11px;color:var(--secondary-text-color)}" +
      ".v{font-size:13px;color:var(--primary-text-color);font-variant-numeric:tabular-nums}" +
      ".empty{padding:16px 0;color:var(--secondary-text-color);font-size:13px}" +
      ".tbl{margin-top:10px;border-top:1px solid var(--divider-color,#444);padding-top:8px}" +
      ".tbl summary{cursor:pointer;font-size:13px;color:var(--primary-text-color);padding:2px 0}" +
      ".tbl table{width:100%;border-collapse:collapse;margin-top:6px;font-size:12px;font-variant-numeric:tabular-nums}" +
      ".tbl th{text-align:left;font-weight:500;color:var(--secondary-text-color);padding:4px 6px;border-bottom:1px solid var(--divider-color,#444)}" +
      ".tbl td{padding:3px 6px;color:var(--primary-text-color);border-bottom:1px solid rgba(127,127,127,.12)}" +
      ".tbl .n{text-align:right}" +
      ".tbl td.r{color:var(--secondary-text-color)}" +
      "</style>"
    );
  }
}

customElements.define("nimbus-forecast-card", NimbusForecastCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "nimbus-forecast-card",
  name: "Nimbus Forecaster",
  description: "Measured history, forecast and forecast range for every Nimbus Load and Power Signal, with how well the model is doing.",
});
