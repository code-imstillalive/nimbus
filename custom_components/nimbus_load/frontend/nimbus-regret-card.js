// nimbus-regret-card.js
//
// 2026-09-01: reconstructs Mark Purcell's own regret-attribution analysis
// (nimbus issue #273, the row-major hourly reconstruction from #297/#310)
// as a live dashboard card, matching the shape of the standalone report
// built the same night -- three trajectories (Reference/idle, Achieved/
// real, Star/oracle) over one real calendar day, plus an hourly regret
// bar chart highlighting exactly where the day's dollar gap came from.
//
// Still calls nimbus_load.compute_quality_report (issue #316/#317) for the
// hourly trajectory arrays (j_ref_hourly/j_ach_hourly/j_star_hourly,
// hourly_regret) -- sensor.nimbus_solver_quality_report (the standalone
// retrospective writer's own table, "the bible" per direct household
// instruction 2026-09-05) only ever stores DAILY totals per date in its
// own `history` attribute, never an hourly breakdown, so the live service
// call is still the only source for the two charts' own line/bar shapes.
//
// Real bug, 2026-09-05: the live service's own _compute_report_for_window()
// genuinely diverges from the table's own numbers for the same real day
// (EPR 71.5% vs the table's 89.9% on 2026-09-04, real root cause not yet
// found -- see 116KAT-HA-AI's CLAUDE.md Live Open Items). Household's own
// directive: "the table for EPR and trend is the bible... charts should
// just display it." Fixed by pulling the 5 headline stat numbers (EPR,
// J_ref, J_ach, J_star, Regret) from sensor.nimbus_solver_quality_report's
// own `history[dateKey]` entry instead of the service response's matching
// top-level fields -- the hourly charts below them still come from the
// live service call and are a genuine, disclosed approximation of shape
// only; only a date not yet present in the table's own history (today, or
// a real gap) falls back to the live service's own totals, flagged as
// such in the sub-header.
//
// Follows the same plain-HTMLElement/shadow-DOM pattern as this
// project's own nimbus-topology-card (nimbus-topology-card.js) rather
// than a framework, for consistency with the household's established
// custom-card style.

class NimbusRegretCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._cache = null; // { dateKey, data }
    this._loading = false;
    this._error = null;
    this._buildStaticShell();
    // Redraws at the container's own current width on resize -- without
    // this, resizing the browser after the initial render (e.g. sidebar
    // toggle, window resize) leaves the canvas's fixed internal
    // resolution mismatched against its new CSS width, reintroducing
    // the exact blur _setupCanvas()'s own fix addresses at draw time.
    if (!this._resizeObserver) {
      this._resizeObserver = new ResizeObserver(() => {
        if (this._cache) this._drawCharts(this._cache.data);
      });
      this._resizeObserver.observe(this);
    }
  }

  disconnectedCallback() {
    if (this._resizeObserver) this._resizeObserver.disconnect();
  }

  set hass(hass) {
    this._hass = hass;
    const dateKey = this._targetDateKey();
    // Real incident, 2026-09-01: fetching on every hass tick whenever
    // there's no successful cache for dateKey turned one bad request
    // (a timezone bug, since fixed) into an unbounded retry storm --
    // hass updates many times a minute, and a permanently-failing
    // fetch never populated _cache, so this fired again and again,
    // each one surfacing its own error toast. _attemptedDateKey tracks
    // "have I already tried this date, success or failure" separately
    // from _cache ("did it succeed") -- only a genuine day rollover or
    // the manual refresh button clears it and allows a new attempt.
    if (this._attemptedDateKey !== dateKey) {
      this._attemptedDateKey = dateKey;
      this._fetch(dateKey);
    }
  }

  getCardSize() {
    return 14;
  }

  _defaultTitle() {
    const daysAgo = Number.isFinite(this._config.days_ago) ? this._config.days_ago : 1;
    if (daysAgo === 1) return "Dispatch Regret — Yesterday";
    return `Dispatch Regret — ${daysAgo} days ago`;
  }

  // config.days_ago (default 1 = "yesterday") lets several instances of
  // this same card, each configured with a different offset, tile a
  // multi-day trend dashboard (2026-09-01 direct ask: "a set of 3 day
  // cards going back 3 days"). Computed in the browser viewer's own
  // local time -- matches the scoring engine's own "most recently
  // fully-elapsed calendar day" semantics closely enough for a display
  // card (the real scorer uses Brisbane time server-side; a viewer in a
  // different timezone would occasionally be off by the tail end of one
  // day right around local midnight -- an acceptable, disclosed
  // tradeoff for a display card, not a metric this project reports
  // numbers from).
  // Real bug, 2026-09-01: toISOString() always converts to UTC before
  // formatting. For a positive UTC offset (Brisbane is +10), "local
  // midnight of the next day" still falls on the PREVIOUS UTC calendar
  // date -- slicing that string silently produced the same date as
  // `start`, tripping the service's own "end must be after start"
  // validation on every card, every time. getFullYear/getMonth/getDate
  // read the LOCAL calendar date directly, with no UTC conversion at
  // all -- the only correct way to do date-only arithmetic here.
  _formatLocalDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  _targetDateKey() {
    const daysAgo = Number.isFinite(this._config.days_ago) ? this._config.days_ago : 1;
    const y = new Date();
    y.setDate(y.getDate() - daysAgo);
    return this._formatLocalDate(y);
  }

  async _fetch(dateKey) {
    if (this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      // Real incident, 2026-09-01: these were built with no timezone
      // offset at all (start) or via toISOString() sliced to look
      // offset-less but actually UTC (end) -- the service parses them
      // with Python's datetime.fromisoformat(), producing an
      // offset-NAIVE datetime that then crashes comparing against
      // dt_util.now() (offset-aware): "can't compare offset-naive and
      // offset-aware datetimes". Explicit +10:00 (Brisbane has no DST)
      // on both ends is what the scoring engine actually expects --
      // matches the exact strings verified working by hand against the
      // real service tonight.
      const start = `${dateKey}T00:00:00+10:00`;
      const endDateObj = new Date(`${dateKey}T00:00:00`);
      endDateObj.setDate(endDateObj.getDate() + 1);
      const end = `${this._formatLocalDate(endDateObj)}T00:00:00+10:00`;
      const result = await this._hass.callService(
        "nimbus_load",
        "compute_quality_report",
        { start, end, allow_partial: false },
        undefined,
        false,
        true
      );
      const payload = result && result.response ? result.response : null;
      if (!payload) throw new Error("no response payload");
      this._cache = { dateKey, data: payload };
    } catch (e) {
      this._error = (e && e.message) || String(e);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _buildStaticShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card {
          padding: 16px 16px 12px;
          background: var(--card-background-color, #181D25);
          color: var(--primary-text-color, #E7EBEF);
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        .head {
          display: flex;
          justify-content: space-between;
          align-items: baseline;
          gap: 12px;
          margin-bottom: 10px;
          flex-wrap: wrap;
        }
        .title { font-size: 1.3rem; font-weight: 600; }
        .sub { font-size: 0.8rem; opacity: 0.65; }
        .stats {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
          gap: 1px;
          background: var(--divider-color, #2A323D);
          border: 1px solid var(--divider-color, #2A323D);
          border-radius: 6px;
          overflow: hidden;
          margin-bottom: 12px;
        }
        .stat {
          background: var(--card-background-color, #181D25);
          padding: 8px 10px;
        }
        .stat .l { font-size: 0.8rem; text-transform: uppercase; opacity: 0.6; letter-spacing: 0.04em; }
        .stat .v { font-size: 1.6rem; font-variant-numeric: tabular-nums; }
        .legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 0.72rem; opacity: 0.75; margin-bottom: 4px; }
        .legend span { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
        .sw { width: 12px; height: 3px; border-radius: 2px; display: inline-block; }
        .sw.dashed { background: repeating-linear-gradient(90deg, #6B7482 0 4px, transparent 4px 7px); }
        .chart-wrap { overflow-x: auto; margin-bottom: 6px; }
        canvas { display: block; width: 100%; }
        .msg { padding: 20px 4px; opacity: 0.7; font-size: 0.9rem; }
        .refresh {
          background: none; border: 1px solid var(--divider-color, #2A323D);
          color: inherit; border-radius: 5px; padding: 4px 10px; font-size: 0.75rem;
          cursor: pointer; opacity: 0.8;
        }
        .refresh:hover { opacity: 1; }
      </style>
      <ha-card>
        <div class="head">
          <div>
            <div class="title">${this._config.title || this._defaultTitle()}</div>
            <div class="sub" id="sub"></div>
          </div>
          <button class="refresh" id="refreshBtn">Refresh</button>
        </div>
        <div id="body"></div>
      </ha-card>
    `;
    this.shadowRoot.getElementById("refreshBtn").addEventListener("click", () => {
      this._cache = null;
      const dateKey = this._targetDateKey();
      this._attemptedDateKey = dateKey;
      this._fetch(dateKey);
    });
  }

  _render() {
    if (!this.shadowRoot) return;
    const subEl = this.shadowRoot.getElementById("sub");
    const bodyEl = this.shadowRoot.getElementById("body");
    if (!subEl || !bodyEl) return;

    if (this._loading && !this._cache) {
      subEl.textContent = "";
      bodyEl.innerHTML = `<div class="msg">Scoring the day…</div>`;
      return;
    }
    if (this._error && !this._cache) {
      subEl.textContent = "";
      bodyEl.innerHTML = `<div class="msg">Couldn't score yesterday: ${this._escape(this._error)}</div>`;
      return;
    }
    if (!this._cache) {
      bodyEl.innerHTML = `<div class="msg">No data yet.</div>`;
      return;
    }

    const d = this._cache.data;
    // Table-first stats (2026-09-05): sensor.nimbus_solver_quality_report's
    // own `history[dateKey]` is the authoritative source -- see this file's
    // own top-of-file comment for why the live service's matching fields
    // can genuinely disagree with it. Falls back to the service response's
    // own totals (flagged in the sub-header) only when this date hasn't
    // been scored into the table yet.
    const tableEntity = this._hass && this._hass.states
      ? this._hass.states["sensor.nimbus_solver_quality_report"]
      : null;
    const tableDay = tableEntity && tableEntity.attributes && tableEntity.attributes.history
      ? tableEntity.attributes.history[this._cache.dateKey]
      : null;
    const stats = tableDay
      ? {
          epr_pct: tableDay.epr * 100,
          j_ref: tableDay.j_ref,
          j_ach: tableDay.j_ach,
          j_star: tableDay.j_star,
          regret_dollars: tableDay.regret_dollars,
        }
      : {
          epr_pct: d.epr_pct,
          j_ref: d.j_ref,
          j_ach: d.j_ach,
          j_star: d.j_star,
          regret_dollars: d.regret_dollars,
        };
    subEl.textContent = this._cache.dateKey + " (Brisbane)"
      + (tableDay ? "" : " — not yet in table, showing live estimate");

    bodyEl.innerHTML = `
      <div class="stats">
        <div class="stat"><div class="l">EPR</div><div class="v">${stats.epr_pct.toFixed(1)}%</div></div>
        <div class="stat"><div class="l">J_ref</div><div class="v">$${stats.j_ref.toFixed(2)}</div></div>
        <div class="stat"><div class="l">J_ach</div><div class="v">$${stats.j_ach.toFixed(2)}</div></div>
        <div class="stat"><div class="l">J_star</div><div class="v">$${stats.j_star.toFixed(2)}</div></div>
        <div class="stat"><div class="l">Regret</div><div class="v">$${stats.regret_dollars.toFixed(2)}</div></div>
      </div>
      <div class="sub" style="margin-bottom:8px;">Charts below show hourly shape only (live solver re-run) -- may not sum exactly to the table totals above.</div>
      <div class="legend">
        <span><i class="sw dashed"></i> Reference</span>
        <span><i class="sw" style="background:#E8A33D"></i> Achieved</span>
        <span><i class="sw" style="background:#4FC3C7"></i> Star (oracle)</span>
      </div>
      <div class="chart-wrap"><canvas id="dispatchCanvas" width="1600" height="380"></canvas></div>
      <div class="chart-wrap"><canvas id="regretCanvas" width="1600" height="220"></canvas></div>
    `;

    this._drawCharts(d);
  }

  _escape(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  _hourlyToArrays(hourlyDict, field) {
    const keys = Object.keys(hourlyDict).sort();
    return keys.map((k) => hourlyDict[k][field]);
  }

  _drawCharts(d) {
    const hours = Array.from({ length: 24 }, (_, i) => i);
    const priceImport = this._hourlyToArrays(d.j_ach_hourly, "import_price_aud_per_kwh");
    const refBatt = this._hourlyToArrays(d.j_ref_hourly, "battery_kw");
    const achBatt = this._hourlyToArrays(d.j_ach_hourly, "battery_kw");
    const starBatt = this._hourlyToArrays(d.j_star_hourly, "battery_kw");
    const regret = hours.map((h) => (d.hourly_regret && d.hourly_regret[String(h)]) || 0);

    const dispatchCanvas = this.shadowRoot.getElementById("dispatchCanvas");
    const regretCanvas = this.shadowRoot.getElementById("regretCanvas");
    if (!dispatchCanvas || !regretCanvas) return;

    const styles = getComputedStyle(this);
    const textDim = styles.getPropertyValue("--secondary-text-color") || "#8B95A3";
    const gridColor = styles.getPropertyValue("--divider-color") || "#2A323D";
    const achievedColor = "#E8A33D";
    const oracleColor = "#4FC3C7";
    const referenceColor = "#6B7482";
    const badColor = "#E8615F";
    const goodColor = "#6FCF97";

    // Explicit logical heights (matching each canvas's own original HTML
    // height="..." attribute) -- see _setupCanvas()'s own comment for why
    // these can no longer be read back from canvas.height itself.
    this._setupCanvas(dispatchCanvas, 380);
    this._setupCanvas(regretCanvas, 220);

    // --- dispatch chart ---
    {
      const ctx = dispatchCanvas.getContext("2d");
      const w = dispatchCanvas._logicalW, h = dispatchCanvas._logicalH;
      // padL widened 46->72: at 18px font, right-aligned "+44kW"-style
      // labels run ~55-60px wide -- anchored at padL-6 with the old,
      // narrower padL left no room before the canvas's own x=0 edge,
      // clipping everything but the last digit. 72 gives real margin.
      const padL = 72, padR = 12, padT = 18, padB = 52;
      const plotW = w - padL - padR, plotH = h - padT - padB;
      const kwAbsMax = Math.max(42, ...refBatt.concat(achBatt, starBatt).map(Math.abs)) * 1.05;
      const priceMax = Math.max(...priceImport) * 1.15 || 1;
      const x = (i) => padL + (plotW * i) / 23;
      const yK = (v) => padT + plotH * (1 - (v + kwAbsMax) / (2 * kwAbsMax));
      const yP = (v) => padT + plotH * (1 - v / priceMax);

      ctx.clearRect(0, 0, w, h);

      ctx.strokeStyle = gridColor;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, yK(0));
      ctx.lineTo(w - padR, yK(0));
      ctx.stroke();

      ctx.beginPath();
      ctx.moveTo(x(0), yP(0));
      hours.forEach((i) => ctx.lineTo(x(i), yP(priceImport[i])));
      ctx.lineTo(x(23), yP(0));
      ctx.closePath();
      ctx.fillStyle = "rgba(232,163,61,0.10)";
      ctx.fill();

      const line = (data, color, dashed) => {
        ctx.beginPath();
        hours.forEach((i, idx) => (idx === 0 ? ctx.moveTo(x(i), yK(data[i])) : ctx.lineTo(x(i), yK(data[i]))));
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        if (dashed) ctx.setLineDash([5, 4]);
        ctx.stroke();
        ctx.setLineDash([]);
      };
      line(refBatt, referenceColor, true);
      line(starBatt, oracleColor, false);
      line(achBatt, achievedColor, false);

      // Real ticks (a short line down from the plot's own bottom edge at
      // each labelled hour), not just floating text -- 2026-09-01 direct
      // ask, matching a real axis rather than unanchored labels.
      const tickY0 = padT + plotH;
      const tickY1 = tickY0 + 8;
      ctx.strokeStyle = textDim;
      ctx.lineWidth = 1.5;
      [0, 6, 12, 18, 23].forEach((i) => {
        ctx.beginPath();
        ctx.moveTo(x(i), tickY0);
        ctx.lineTo(x(i), tickY1);
        ctx.stroke();
      });
      ctx.fillStyle = textDim;
      ctx.font = "18px sans-serif";
      // Edge ticks (00:00 / 23:00) sit exactly on the plot's own left/
      // right boundary -- centered text there spills half its own
      // width off the canvas edge. Left-align the first tick and
      // right-align the last so every label stays fully inside the
      // canvas; the three interior ticks stay centered on their mark.
      [0, 6, 12, 18, 23].forEach((i) => {
        ctx.textAlign = i === 0 ? "left" : i === 23 ? "right" : "center";
        ctx.fillText(String(i).padStart(2, "0") + ":00", x(i), h - 6);
      });
      ctx.textAlign = "right";
      ctx.fillText("+" + Math.round(kwAbsMax) + "kW", padL - 6, yK(kwAbsMax) + 4);
      ctx.fillText("-" + Math.round(kwAbsMax) + "kW", padL - 6, yK(-kwAbsMax) + 4);
    }

    // --- regret chart ---
    {
      const ctx = regretCanvas.getContext("2d");
      const w = regretCanvas._logicalW, h = regretCanvas._logicalH;
      const padL = 46, padR = 12, padT = 14, padB = 48;
      const plotW = w - padL - padR, plotH = h - padT - padB;
      const maxAbs = Math.max(0.5, ...regret.map(Math.abs)) * 1.2;
      const barW = (plotW / 24) * 0.6;
      const x = (i) => padL + (plotW * i) / 23;
      const yZero = padT + plotH / 2;
      const scale = plotH / 2 / maxAbs;

      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = gridColor;
      ctx.beginPath();
      ctx.moveTo(padL, yZero);
      ctx.lineTo(w - padR, yZero);
      ctx.stroke();

      hours.forEach((i) => {
        const v = regret[i];
        const bh = Math.abs(v) * scale;
        ctx.fillStyle = v >= 0 ? badColor : goodColor;
        if (v >= 0) ctx.fillRect(x(i) - barW / 2, yZero - bh, barW, bh);
        else ctx.fillRect(x(i) - barW / 2, yZero, barW, bh);
      });

      const tickY0 = padT + plotH;
      const tickY1 = tickY0 + 8;
      ctx.strokeStyle = textDim;
      ctx.lineWidth = 1.5;
      [0, 6, 12, 18, 23].forEach((i) => {
        ctx.beginPath();
        ctx.moveTo(x(i), tickY0);
        ctx.lineTo(x(i), tickY1);
        ctx.stroke();
      });
      ctx.fillStyle = textDim;
      ctx.font = "18px sans-serif";
      [0, 6, 12, 18, 23].forEach((i) => {
        ctx.textAlign = i === 0 ? "left" : i === 23 ? "right" : "center";
        ctx.fillText(String(i).padStart(2, "0") + ":00", x(i), h - 6);
      });
    }
  }

  // Real bug, 2026-09-01: the canvas always drew at a fixed internal
  // resolution (the HTML width="..." attribute, e.g. 1600) while its
  // CSS width was "100%" -- fine on a narrow card where the container
  // is close to that fixed width, but once cards went full-width
  // (max_columns: 1) the browser started stretching the ALREADY-
  // rendered pixels to fill a much wider container, producing exactly
  // the blurry/stretched text this was reported as. Fix: measure the
  // canvas's own actual, current CSS-computed width (after the "100%"
  // stylesheet rule has already applied to it, since it's inserted
  // into the same shadow root the style lives in) and set the internal
  // pixel resolution to genuinely match that, every render -- the
  // canvas is only ever asked to stretch by exactly devicePixelRatio,
  // never by an arbitrary, unrelated CSS width.
  //
  // Second real bug, 2026-09-03 (household report: "regret views
  // collapse onto each other when viewing scale is not 100%, lose all
  // content but the top table row"). `logicalH` used to be read back
  // from `canvas.height` -- but that property holds this SAME
  // function's own previous output (already multiplied by `dpr` below),
  // not a stable source value. At exactly 100% zoom (dpr=1) that's
  // harmless (1*1=1 forever), but at any other zoom this function is
  // called more than once (a ResizeObserver firing, the refresh button,
  // a second render), each call re-multiplied the ALREADY-scaled height
  // by `dpr` again -- a runaway compounding loop, not a one-time scale.
  // Zoomed below 100% (dpr<1) this shrinks the canvas toward zero over a
  // few redraws, exactly matching "collapses, loses all content but the
  // static HTML table above it" (the .stats grid isn't a canvas, so it
  // never collapsed). Zoomed above 100% it would instead blow the
  // canvas up without bound. Width never had this problem because
  // `getBoundingClientRect().width` re-measures real DOM layout fresh
  // every call, independent of any earlier scaling -- it was only
  // height that fed its own output back in as if it were new input.
  // Fix: height is now an explicit parameter (each call site passes its
  // canvas's own fixed design height, matching the original HTML
  // height="..." attribute), never re-derived from the canvas's own,
  // already-mutated `.height` property -- making this function
  // idempotent no matter how many times it's called or what `dpr` is.
  _setupCanvas(canvas, logicalH) {
    const dpr = window.devicePixelRatio || 1;
    const logicalW = Math.round(canvas.getBoundingClientRect().width) || canvas.width;
    canvas._logicalW = logicalW;
    canvas._logicalH = logicalH;
    canvas.style.width = "100%";
    canvas.style.height = logicalH + "px";
    canvas.width = Math.round(logicalW * dpr);
    canvas.height = Math.round(logicalH * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
}

customElements.define("nimbus-regret-card", NimbusRegretCard);

// Card picker registration -- matches nimbus-topology-card's own
// convention in this same directory.
window.customCards = window.customCards || [];
window.customCards.push({
  type: "nimbus-regret-card",
  name: "Nimbus Dispatch Regret",
  description: "Yesterday's battery dispatch vs. a perfect-foresight oracle, hour by hour.",
});
