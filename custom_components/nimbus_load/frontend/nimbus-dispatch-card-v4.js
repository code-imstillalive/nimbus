
class NimbusDispatchCardV4 extends HTMLElement {
  setConfig(config) {
    this.config = config || {};
    // Nimbus issue #364 (Mark Purcell): this card shipped with 8 real
    // household-specific entity_ids hardcoded directly in the source --
    // works only on this exact devhub install, unusable by any other
    // Nimbus HACS install. Resolved from card config instead, same "no
    // hardcoding" convention the rest of this project follows -- every
    // household (including this one) now configures these explicitly in
    // the dashboard YAML `config:` block (see docs/dashboards.md).
    // CORE fields: the card degrades gracefully (idle/zero/unknown) if
    // left unset, but is only meaningfully USEFUL once these are real.
    this._modeEntity = this.config.mode_select_entity || "";
    this._armedEntity = this.config.armed_entity || "";
    this._battEntity = this.config.battery_power_entity || "";
    this._socEntity = this.config.battery_soc_entity || "";
    this._gridEntity = this.config.grid_power_entity || "";
    this._solarEntity = this.config.solar_power_entity || "";
    // OPTIONAL fields: no default at all -- an unset optional entity
    // hides its own UI element entirely rather than showing a
    // permanently-red/unknown chip for something the household never
    // had in the first place (e.g. an EV charger, or a second node's
    // own job-health monitor).
    this._evEntity = this.config.ev_charger_power_entity || null;
    this._p2pThresholdEntity = this.config.p2p_threshold_entity || null;
    // nimbus issue #388 (Mark Purcell): docs/dashboards.md documents
    // battery_power_entity as "positive = discharging", but a SigEnergy-
    // style install's own real sensor is the opposite convention -- the
    // exact reason the Solver has its own solver_battery_power_positive_
    // is_charge flag (#299/#307). Without this, every charging period
    // renders as DISCHARGING (and the 18h "Actual" history line mirrors
    // against the plan) on any install using that convention, with no
    // way to fix it short of a manual template-sensor workaround.
    // Explicit override, undefined by default (no hardcoded true/false
    // here) -- only set when a household's own sensor config entity is
    // unavailable/stale for some reason. The normal path (no override
    // configured) auto-detects from the Solver's own
    // sensor.nimbus_solver_config attributes in _battSign() below, so the
    // card agrees with the plan it overlays by construction, with zero
    // extra config for the common case.
    this._battPositiveIsChargeOverride =
      typeof this.config.battery_power_positive_is_charge === 'boolean'
        ? this.config.battery_power_positive_is_charge
        : null;
    // Arbitrary-length list of {entity, label} health/status chips shown
    // in the footer -- generalizes what used to be two hardcoded
    // "NUC1 writer"/"NUC2 writer" chips (a two-node-failover concept
    // specific to this one household's own architecture) into something
    // any install can configure zero, one, or several of.
    this._healthChecks = Array.isArray(this.config.health_checks)
      ? this.config.health_checks.filter((h) => h && h.entity)
      : [];
  }
  getCardSize() { return 12; }

  set hass(hass) {
    this._hass = hass;
    this._maybeFetchHistory();
    this._render();
  }

  // Clears the render throttle so the very next hass update (arriving once
  // this service call actually lands) renders immediately instead of
  // waiting out the background-churn throttle window -- keeps direct user
  // actions feeling instant while still throttling unrelated hass ticks.
  _clearRenderThrottle() {
    if (this._pendingRenderTimer) { clearTimeout(this._pendingRenderTimer); this._pendingRenderTimer = null; }
    this._lastRenderAt = 0;
  }

  _setMode(mode) {
    this._clearRenderThrottle();
    if (!this._modeEntity) return;
    this._hass.callService('input_select', 'select_option', {entity_id: this._modeEntity, option: mode});
  }

  _toggleArmed() {
    this._clearRenderThrottle();
    if (!this._armedEntity) return;
    const armed = this._hass.states[this._armedEntity];
    const isOn = armed && armed.state === 'on';
    this._hass.callService('input_boolean', isOn ? 'turn_off' : 'turn_on', {entity_id: this._armedEntity});
  }

  _setRisk(entityId, value) {
    this._clearRenderThrottle();
    this._hass.callService('number', 'set_value', {entity_id: entityId, value});
  }

  // nimbus_load.solve_now (services.yaml) is a real, no-argument,
  // hub-level service -- no entity_id/target needed, unlike every
  // other service call in this file. _solving/_render() give the
  // button real visual feedback ("Solving…", disabled) for the
  // duration of the call instead of looking like a no-op click while
  // the real solve (up to ~2 minutes on this household's own solve
  // times, see solver_writer.py's own acquire_lock() comment) runs.
  async _solveNow() {
    if (this._solving) return;
    this._solving = true;
    this._clearRenderThrottle();
    this._render();
    try {
      await this._hass.callService('nimbus_load', 'solve_now', {});
    } catch (err) {
      console.error('nimbus-dispatch-card-v4: solve_now failed', err);
    } finally {
      this._solving = false;
      this._clearRenderThrottle();
      this._render();
    }
  }

  _maybeFetchHistory() {
    const now = Date.now();
    if (this._historyFetchedAt && now - this._historyFetchedAt < 5 * 60 * 1000) return;
    this._historyFetchedAt = now;
    const start = new Date(now - 18 * 3600 * 1000).toISOString();
    if (!this._battEntity) return;
    this._hass.callApi('GET', 'history/period/' + start + '?filter_entity_id=' + this._battEntity + '&minimal_response')
      .then(data => {
        const series = (data && data[0]) || [];
        // nimbus issue #388: the raw history API always returns the
        // sensor's own native sign -- apply the same flip as the live
        // reading (_battSign()) so the "Actual" line overlaid on the
        // timeline matches the plan's own convention instead of being
        // mirrored against it on a positive-is-charge install.
        const sign = this._battSign();
        this._actualHistory = series
          .map(pt => [new Date(pt.last_changed || pt.lu * 1000).getTime(), parseFloat(pt.state) * sign])
          .filter(pt => !isNaN(pt[1]));
        this._render();
      })
      .catch(() => { this._actualHistory = this._actualHistory || []; });
  }

  _num(entityId, fallback) {
    const e = this._hass.states[entityId];
    const v = e ? parseFloat(e.state) : NaN;
    return isNaN(v) ? fallback : v;
  }

  // Real entities on this project can be natively W or kW depending on
  // which sensor -- read the entity's own unit_of_measurement rather than
  // assume, and convert to kW consistently (already-documented, real gotcha
  // for sensor.combined_total_dc_power specifically, which is native W).
  _numAsKw(entityId, fallback) {
    const e = this._hass.states[entityId];
    if (!e) return fallback;
    const v = parseFloat(e.state);
    if (isNaN(v)) return fallback;
    const unit = ((e.attributes && e.attributes.unit_of_measurement) || '').toLowerCase();
    return unit === 'w' ? v / 1000 : v;
  }

  _fmtNum(v, decimals, suffix) {
    return (isNaN(v) ? '—' : v.toFixed(decimals)) + (suffix || '');
  }

  // nimbus issue #388: the sign multiplier to apply to whatever
  // battery_power_entity's raw sensor reports, so this card's own
  // "positive = discharging" convention (docs/dashboards.md) holds
  // regardless of which way the household's own hardware reports it.
  // The explicit card-config override (set in setConfig()) always wins
  // when present; otherwise this reads the Solver's own
  // solver_battery_power_positive_is_charge flag straight off
  // sensor.nimbus_solver_config's attributes -- the same hub, the same
  // flag the Solver itself already uses to normalize this exact sensor
  // (#299/#307), so the card agrees with the plan it overlays by
  // construction rather than guessing independently. Missing/unavailable
  // sensor.nimbus_solver_config (e.g. mid-restart) defaults to `false`
  // (no sign flip), matching every install's behaviour before this fix.
  _battSign() {
    if (this._battPositiveIsChargeOverride !== null) {
      return this._battPositiveIsChargeOverride ? -1 : 1;
    }
    const cfg = this._hass && this._hass.states['sensor.nimbus_solver_config'];
    const positiveIsCharge = !!(cfg && cfg.attributes && cfg.attributes.solver_battery_power_positive_is_charge);
    return positiveIsCharge ? -1 : 1;
  }

  // Real absolute sell rate = spot + P2P premium (bonus_price is deliberately
  // just the incremental premium, per nimbus's own field-semantics convention
  // -- never show it bare, always spot+bonus). Distinguishes "spot only" from
  // a genuine active P2P period so the reasoning text never implies P2P
  // revenue when there isn't any.
  _fmtSell(p) {
    if (!p) return '—';
    const spot = parseFloat(p.export_price) || 0;
    const bonus = parseFloat(p.bonus_price) || 0;
    const total = (spot + bonus) * 100;
    if (bonus > 0.01) {
      return total.toFixed(1) + 'c/kWh (spot ' + (spot * 100).toFixed(1) + 'c + P2P ' + (bonus * 100).toFixed(1) + 'c)';
    }
    return total.toFixed(1) + 'c/kWh (spot only, no P2P this period)';
  }

  _render(force) {
    const hass = this._hass;
    if (!hass) return;
    // Real root cause of "scroll takes a million clicks to move a line"
    // (2026-09-05): the scroll-position restore alone didn't fix this --
    // `set hass()` fires on essentially every state change anywhere in HA,
    // which for a live dashboard can be several times a SECOND. Restoring
    // scrollTop after each rebuild is correct but each rebuild still tears
    // out and recreates the actual scrollable DOM node, which resets the
    // browser's own scroll/momentum physics every single time -- so a
    // mouse-wheel notch or scrollbar click barely gets a chance to move
    // before the next rebuild interrupts it. Real fix: throttle full
    // rebuilds to at most once every 3s. A direct user action (mode click,
    // kill switch, slider release) still re-renders immediately via
    // force=true, so the card never feels unresponsive to something the
    // user actually did -- only background hass churn gets throttled.
    if (this._sliderDragging) return;
    const now = Date.now();
    if (!force && this._lastRenderAt && now - this._lastRenderAt < 3000) {
      if (!this._pendingRenderTimer) {
        const wait = 3000 - (now - this._lastRenderAt);
        this._pendingRenderTimer = setTimeout(() => {
          this._pendingRenderTimer = null;
          this._render(true);
        }, wait);
      }
      return;
    }
    this._lastRenderAt = now;
    const prevWrap = this._built ? this.shadowRoot.querySelector('.ftable-wrap') : null;
    const prevScrollTop = prevWrap ? prevWrap.scrollTop : 0;
    // 2026-09-07: direct household report -- horizontal scroll snapped
    // back to the left on every re-render ("magnetic, pulls to the left
    // every second"). Same real cause as the vertical version this file
    // already fixed (2026-09-05, see this function's own top-of-function
    // comment): every re-render tears out and rebuilds .ftable-wrap's DOM
    // node, resetting its scroll position -- but that earlier fix only
    // ever captured/restored scrollTop, never scrollLeft, so the
    // horizontal axis was still silently broken every ~3s throttled
    // rebuild. Same capture-before/restore-after pattern, just the other
    // axis too.
    const prevScrollLeft = prevWrap ? prevWrap.scrollLeft : 0;

    const modeEnt = this._modeEntity ? hass.states[this._modeEntity] : undefined;
    const mode = modeEnt ? modeEnt.state : 'Self-Consume';
    const armedEnt = this._armedEntity ? hass.states[this._armedEntity] : undefined;
    const armed = armedEnt ? armedEnt.state === 'on' : false;
    const socEnt = this._socEntity ? hass.states[this._socEntity] : undefined;
    const soc = socEnt ? parseFloat(socEnt.state) : 0;
    const minSoc = this._num('number.nimbus_solver_battery_min_soc_percent', 2);
    const maxSoc = this._num('number.nimbus_solver_battery_max_soc_percent', 100);
    const fcEnt = hass.states['sensor.nimbus_solver_battery_forecast'];
    const fc = (fcEnt && fcEnt.attributes && fcEnt.attributes.forecast) || [];
    const p0 = fc[0];
    const solverStatus = fcEnt ? (fcEnt.attributes.status || '?') : '?';
    const clamped = fcEnt ? fcEnt.attributes.n_clamped_periods : undefined;
    const solveSecs = fcEnt ? fcEnt.attributes.solve_seconds : undefined;
    const generatedAt = fcEnt ? fcEnt.attributes.generated_at : undefined;
    const healthChips = this._healthChecks.map((h) => ({label: h.label || h.entity, ent: hass.states[h.entity]}));

    let dir = 'NO PLAN YET', dirColor = '#9aa0ac', reasoning = 'No Solver plan yet.', bkw = 0, isIdle = true;
    // Direct household correction (2026-09-05): a single bare "5.7 kW" next
    // to the status heading was ambiguous -- "no one knows" what it means.
    // When set (real/disarmed branch only, where the number is genuinely
    // measured rather than a clearly-labeled plan like "PLANNED DISCHARGE"),
    // this replaces the single value with real grid/solar/battery shown
    // together so the number is never ambiguous again.
    let kwTriple = null;

    // Real dichotomy (2026-09-05, direct household design): ARMED is the
    // master kill switch -- disarmed means nothing below is followed at
    // all, exactly matching HAEO's own single on/off automation toggle.
    // Once armed, "Automatic" mode blindly follows the Nimbus Solver's own
    // plan (labeled PLANNED, same computation as before this change) --
    // any other mode is a genuine MANUAL override and must never claim to
    // be following the plan, even though the gauge/chart above still show
    // what the plan IS for reference. This card's own kill switch is a
    // LOCAL, devhub-only rehearsal (input_boolean.devhub_nimbus_dispatch_
    // armed_rehearsal) -- devhub has no inverter, so arming it here can
    // never move anything real. The real switch is NUC1's own
    // input_boolean.nimbus_live_dispatch_armed, gating the already-built
    // automation.nimbus_live_dispatch, unchanged and untouched by this
    // card -- deploying "for real" means reusing that automation exactly
    // as it already exists, not building a new one.
    if (!armed) {
      // Second direct household correction (2026-09-05): the big status
      // heading shouldn't say "DISARMED" at all -- that's already shown by
      // the small ARMED/DISARMED pill next to the kill switch. This
      // heading's job is to describe what the real house is ACTUALLY doing
      // right now, same as it does for every other mode -- CHARGING (SOLAR),
      // CHARGING (GRID), DISCHARGING, or SELF-CONSUME, read from real, live
      // measured sensors (never the Solver's plan, since dispatch is off).
      const realBatt = this._num(this._battEntity, 0) * this._battSign();
      const realGrid = this._num(this._gridEntity, 0);
      const realSolarKw = this._numAsKw(this._solarEntity, 0);
      const realEv = this._evEntity ? this._num(this._evEntity, 0) : 0;
      bkw = realBatt;
      kwTriple = {grid: realGrid, solar: realSolarKw, batt: realBatt};
      const evActive = realEv > 0.05;
      isIdle = Math.abs(realBatt) <= 0.05 && !evActive;
      if (realBatt > 0.05) {
        dir = 'DISCHARGING' + (evActive ? ' + EV CHARGING' : ''); dirColor = '#3ddc84';
      } else if (realBatt < -0.05) {
        // Real bug, 2026-09-06 (household live report): a flat 0.05kW
        // grid threshold correctly detects genuine nighttime grid-charge
        // (solar=0) but mislabels a high-solar day's own measurement
        // noise/rounding as "CHARGING (GRID)" -- confirmed live: 8.7kW
        // solar, 4.7kW charging, 0.2kW grid (>0.05 threshold) labeled
        // GRID when NUC1's own card correctly showed SOLAR for the
        // identical real, mirrored sensors. Grid must now also be a real
        // fraction of whatever solar is producing, not just above the
        // absolute noise floor -- at solar=0 this reduces to the exact
        // same nighttime behaviour as before (realSolarKw*0.05 = 0).
        const fromGrid = realGrid > 0.05 && realGrid > realSolarKw * 0.05;
        dir = (fromGrid ? 'CHARGING (GRID)' : 'CHARGING (SOLAR)') + (evActive ? ' + EV' : '');
        dirColor = fromGrid ? '#4fa3ff' : '#ffb340';
      } else if (evActive) {
        // Battery itself idle, but the EV charger drawing power on its own
        // is still real, genuine "charging" activity -- direct household
        // ask: this must never collapse into a plain SELF-CONSUME label.
        dir = 'CHARGING (EV)'; dirColor = '#4fa3ff';
      } else {
        dir = 'SELF-CONSUME'; dirColor = '#9aa0ac';
      }
      const battNote = realBatt > 0.05
        ? ('the battery is discharging ' + realBatt.toFixed(1) + ' kW')
        : (realBatt < -0.05 ? ('the battery is charging ' + Math.abs(realBatt).toFixed(1) + ' kW') : 'the battery is idle');
      const evNote = realEv > 0.05 ? (', and the EV charger is drawing ' + realEv.toFixed(1) + ' kW') : '';
      // Direct household correction: drop the meta-commentary about
      // rehearsal/devhub entirely -- just say what's actually happening.
      reasoning = 'Dispatch is off: ' + battNote + evNote + '.';
    } else if (mode === 'Automatic') {
      if (p0) {
      bkw = parseFloat(p0.battery_kw) || 0;
      const sellLabel = this._fmtSell(p0);
      const imp = p0.import_price_raw * 100;
      const solarKw = parseFloat(p0.solar_kw) || 0;
      const loadKw = parseFloat(p0.load_kw) || 0;
      const dischargeKw = bkw > 0 ? bkw : 0;
      const chargeKw = bkw < 0 ? -bkw : 0;
      // Energy balance across all three sources: grid + solar + battery-discharge = load + battery-charge + export.
      const netGrid = loadKw + chargeKw - solarKw - dischargeKw; // >0 => importing, <0 => exporting
      const gridImportKw = Math.max(0, netGrid);
      const gridExportKw = Math.max(0, -netGrid);

      if (bkw > 0.05) {
        dir = 'PLANNED DISCHARGE'; dirColor = '#3ddc84'; isIdle = false;
        if (gridExportKw > 0.05) {
          reasoning = 'Battery discharging ' + dischargeKw.toFixed(1) + ' kW plus ' + solarKw.toFixed(1) + ' kW solar covers the ' + loadKw.toFixed(1) + ' kW load with ' + gridExportKw.toFixed(1) + ' kW left to export at ' + sellLabel + ' - worth more now than buying it back later at ' + imp.toFixed(1) + 'c/kWh.';
        } else if (gridImportKw > 0.05) {
          reasoning = 'Battery discharging ' + dischargeKw.toFixed(1) + ' kW plus ' + solarKw.toFixed(1) + ' kW solar still leaves ' + gridImportKw.toFixed(1) + ' kW of the ' + loadKw.toFixed(1) + ' kW load coming from the grid at ' + imp.toFixed(1) + 'c/kWh - the plan still prefers discharging over holding charge here.';
        } else {
          reasoning = 'Battery discharging ' + dischargeKw.toFixed(1) + ' kW plus ' + solarKw.toFixed(1) + ' kW solar exactly covers the ' + loadKw.toFixed(1) + ' kW load - no grid import or export needed.';
        }
      } else if (bkw < -0.05) {
        isIdle = false;
        if (gridImportKw < 0.05) {
          dir = 'PLANNED CHARGE (SOLAR)'; dirColor = '#ffb340';
          reasoning = 'Solar (' + solarKw.toFixed(1) + ' kW) covers the ' + loadKw.toFixed(1) + ' kW load with ' + chargeKw.toFixed(1) + ' kW spare charging the battery for free' + (gridExportKw > 0.05 ? (' and ' + gridExportKw.toFixed(1) + ' kW still exported') : '') + ' - no grid import involved.';
        } else {
          let bestSell = -Infinity, bestIdx = -1;
          for (let i = 1; i < fc.length; i++) {
            const v = (parseFloat(fc[i].export_price) || 0) + (parseFloat(fc[i].bonus_price) || 0);
            if (v > bestSell) { bestSell = v; bestIdx = i; }
          }
          const bestSellLabel = bestIdx >= 0 ? this._fmtSell(fc[bestIdx]) : null;
          const bestSellC = bestSell > -Infinity ? bestSell * 100 : null;
          const solarInvolved = solarKw > 0.05;
          dir = solarInvolved ? 'PLANNED CHARGE (SOLAR + GRID)' : 'PLANNED CHARGE (GRID)';
          dirColor = solarInvolved ? '#a78bfa' : '#4fa3ff';
          const solarNote = solarInvolved ? (solarKw.toFixed(1) + ' kW solar plus ') : '';
          if (bestSellC !== null && bestSellC > imp) {
            const hoursAhead = (bestIdx >= 0 && fc[bestIdx].time) ? Math.round((new Date(fc[bestIdx].time).getTime() - new Date(p0.time).getTime()) / 3600000) : null;
            reasoning = solarNote + gridImportKw.toFixed(1) + ' kW of grid import at ' + imp.toFixed(1) + 'c/kWh is charging the battery to store energy the plan expects to sell for up to ' + bestSellLabel + (hoursAhead !== null ? ' around +' + hoursAhead + 'h' : '') + ' - the spread covers the round trip.';
          } else {
            reasoning = solarNote + gridImportKw.toFixed(1) + ' kW of grid import at ' + imp.toFixed(1) + 'c/kWh is charging the battery - current export value (' + sellLabel + ') doesn\'t justify this on price alone, so the solver is likely holding reserve rather than chasing an arbitrage.';
          }
        }
      } else {
        dir = 'PLANNED SELF-CONSUME'; dirColor = '#9aa0ac'; isIdle = true;
        if (gridImportKw > 0.05) {
          reasoning = 'Solar (' + solarKw.toFixed(1) + ' kW) covers part of the ' + loadKw.toFixed(1) + ' kW load, with ' + gridImportKw.toFixed(1) + ' kW topped up from the grid at ' + imp.toFixed(1) + 'c/kWh - battery is idle, neither charging nor discharging.';
        } else if (gridExportKw > 0.05) {
          reasoning = 'Solar (' + solarKw.toFixed(1) + ' kW) exceeds the ' + loadKw.toFixed(1) + ' kW load with ' + gridExportKw.toFixed(1) + ' kW exported at ' + sellLabel + ' - battery is idle, neither charging nor discharging.';
        } else {
          reasoning = 'Solar (' + solarKw.toFixed(1) + ' kW) matches the ' + loadKw.toFixed(1) + ' kW load directly - no grid import/export, battery idle.';
        }
      }
      }
    } else {
      // A genuine manual override -- armed, but explicitly NOT following
      // the Solver's plan. Never borrow the PLANNED reasoning text here,
      // or this silently implies the plan is being followed when it isn't.
      isIdle = (mode === 'Self-Consume' || mode === 'Preserve');
      const manualColors = {'Self-Consume': '#9aa0ac', 'Charge': '#4fa3ff', 'Discharge': '#3ddc84', 'Preserve': '#ffb340'};
      dir = 'MANUAL: ' + mode.toUpperCase();
      dirColor = manualColors[mode] || '#e8eaf0';
      reasoning = 'Manual override selected (' + mode + ') -- this does NOT follow the Nimbus plan shown in the gauge/timeline below. Switch mode back to Automatic to resume following the Solver blindly.';
    }

    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const gaugePct = clamp((soc - minSoc) / Math.max(1, (maxSoc - minSoc)), 0, 1);
    const gAngle = 180 - gaugePct * 180;
    const gRad = gAngle * Math.PI / 180;
    const gcx = 125, gcy = 125, gr = 95;
    const needleX = gcx + gr * Math.cos(gRad);
    const needleY = gcy - gr * Math.sin(gRad);
    // Colour by position in the real min-max range (not absolute SoC%) so a
    // household with a high min-SoC floor still sees red near ITS OWN empty,
    // not literal 0% -- red near empty, yellow through the middle, green
    // near full (2026-09-05, direct household ask).
    const gaugeColor = gaugePct < 0.15 ? '#ff5a5a' : (gaugePct > 0.85 ? '#3ddc84' : '#ffd54f');
    const gArcLen = Math.PI * gr;

    // Real, always-visible tri-colour gauge track (2026-09-05, direct
    // household correction: "tri coloured as it goes down" -- a single
    // dynamic-colour fill that only ever showed ONE colour at a time was
    // not what was asked for. Same severity-band convention already
    // established elsewhere in this project's gauges (red/yellow/green
    // bands + a needle) -- three fixed arcs spanning the whole track,
    // always rendered regardless of current SoC, with the needle (drawn
    // separately, unchanged) showing where the real value sits among them.
    const gaugeBandDef = [[0, 0.15, '#ff5a5a'], [0.15, 0.85, '#ffd54f'], [0.85, 1, '#3ddc84']];
    const gaugeBands = gaugeBandDef.map(([from, to, color]) => {
      const segLen = (to - from) * gArcLen;
      const offset = -(from * gArcLen);
      return '<path d="M 30 125 A ' + gr + ' ' + gr + ' 0 0 1 220 125" fill="none" stroke="' + color + '" stroke-width="14" stroke-linecap="butt"' +
        ' stroke-dasharray="' + segLen + ' ' + gArcLen + '" stroke-dashoffset="' + offset + '" opacity="0.9"/>';
    }).join('');

    // Direct household ask (2026-09-07, annotated screenshot): the chart
    // only shows 3 real days (72h) now, not the full 96h forecast --
    // fewer days sharing the same horizontal space means each real day
    // gets more width, and the y-axis (below) scales to what's actually
    // visible instead of a possibly-off-screen 4th-day extreme, reading
    // less flat/horizontally-squeezed. This clips a CHART-ONLY view --
    // fc itself stays the full forecast for the table and every other
    // reader below, unaffected.
    const CHART_HORIZON_HOURS = 72;
    const chartCutoffMs = Date.now() + CHART_HORIZON_HOURS * 3600000;
    const fcChart = fc.filter(p => new Date(p.time).getTime() <= chartCutoffMs);
    // Direct household ask (2026-09-07): "timeline numbers need 25% extra
    // height" -- the dispatch-plan chart itself (not the table). 220 -> 275
    // (+25%) directly, simplest possible fix: more real plot height for
    // the same width, no change to width/padding/label spacing.
    const TW = 1000, TH = 275, padL = 46, padR = 20, padT = 16, padB = 34;
    const plotW = TW - padL - padR, plotH = TH - padT - padB;
    const nowMs = Date.now();
    const actual = this._actualHistory || [];
    const times = fcChart.map(p => new Date(p.time).getTime());
    const tMin = actual.length ? Math.min(actual[0][0], times[0] || nowMs) : (times[0] || nowMs - 3600000);
    const tMax = times.length ? times[times.length - 1] : nowMs + CHART_HORIZON_HOURS * 3600000;
    const xOf = t => padL + ((t - tMin) / Math.max(1, (tMax - tMin))) * plotW;
    const maxAbsKw = Math.max(1, ...fcChart.map(p => Math.abs(p.battery_kw || 0)), ...actual.map(a => Math.abs(a[1] || 0)));
    const yZero = padT + plotH * 0.55;
    const yScale = (plotH * 0.42) / maxAbsKw;
    const yOfKw = kw => yZero - kw * yScale;
    const yOfSoc = s => padT + plotH - (s / 100) * plotH;

    let areaPath = '';
    if (fcChart.length) {
      areaPath = 'M ' + xOf(times[0]) + ' ' + yZero;
      fcChart.forEach((p, i) => { areaPath += ' L ' + xOf(times[i]) + ' ' + yOfKw(p.battery_kw || 0); });
      areaPath += ' L ' + xOf(times[times.length - 1]) + ' ' + yZero + ' Z';
    }
    let socPath = '';
    fcChart.forEach((p, i) => { socPath += (i === 0 ? 'M ' : ' L ') + xOf(times[i]) + ' ' + yOfSoc(p.soc_pct || 0); });
    let actualPath = '';
    actual.forEach((a, i) => { actualPath += (i === 0 ? 'M ' : ' L ') + xOf(a[0]) + ' ' + yOfKw(a[1]); });

    // P2P highlight bands (2026-09-05, direct household ask): shade every
    // period where bonus_price is genuinely active (the incremental P2P
    // premium over spot -- see _fmtSell()'s own comment on this field's
    // semantics), so the P2P window is visible directly on this timeline
    // without cross-referencing the Solver tab. One rect per period rather
    // than merging runs -- adjacent active periods render as one
    // continuous band anyway, and this stays correct even if the P2P
    // window is ever non-contiguous (multiple blocks) in the future.
    let p2pBands = '';
    for (let i = 0; i < fcChart.length; i++) {
      const bonus = parseFloat(fcChart[i].bonus_price) || 0;
      if (bonus <= 0.01) continue;
      const x1 = xOf(times[i]);
      const x2 = xOf(times[i + 1] !== undefined ? times[i + 1] : times[i] + 5 * 60000);
      if (x2 <= x1) continue;
      p2pBands += '<rect x="' + x1 + '" y="' + padT + '" width="' + (x2 - x1) + '" height="' + plotH + '" fill="#ffd54f" opacity="0.14"/>';
    }

    const nowX = xOf(nowMs);
    const hourMarks = [];
    for (let h = 0; h <= CHART_HORIZON_HOURS; h += 3) {
      const t = nowMs + h * 3600000;
      if (t <= tMax) hourMarks.push({x: xOf(t), label: h === 0 ? 'now' : '+' + h + 'h'});
    }

    // Compact top-bar mode selector (2026-09-05, direct household correction,
    // stated repeatedly and finally landed here): the old tall stacked-icon
    // button row (with its own section-label) duplicated exactly what the
    // ARMED/DISARMED pill text already said ("having Self-Consume near the
    // button when Self-Consume has another button nearby is redundant") and
    // cost real vertical space the forecast table below needed. Now a single
    // small inline row of abbreviated chips, living in the top bar itself
    // next to the pill/kill-switch -- the chip's own highlight IS the mode
    // indicator, so the pill no longer repeats the mode name (see below).
    // Order matters here, per direct household correction: Automatic on the
    // LEFT, Self-Consume on the RIGHT -- deliberately so SC (the chip) never
    // sits immediately next to anything else already implying self-consume
    // (e.g. the DISARMED reasoning text), which read as a visual duplicate.
    const modes = ['Automatic', 'Charge', 'Discharge', 'Preserve', 'Self-Consume'];
    const modeAbbr = {'Self-Consume': 'SC', 'Charge': 'CH', 'Discharge': 'DIS', 'Preserve': 'PRE', 'Automatic': 'AUTO'};
    const modeButtons = modes.map(m => (
      '<button class="mode-chip ' + (m === mode ? 'active' : '') + '" data-mode="' + m + '" title="' + m + '">' + modeAbbr[m] + '</button>'
    )).join('');

    const okColor = e => (e && e.state === 'ok') ? '#3ddc84' : '#ff5a5a';
    const statusColor = solverStatus === 'optimal' ? '#3ddc84' : '#ff5a5a';
    const lastSolvedStr = generatedAt ? new Date(generatedAt).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) : '?';

    // Risk aversion sliders (2026-09-05, direct household ask, second time --
    // real, live number.nimbus_solver_* entities on THIS devhub instance,
    // confirmed via ha_search before wiring, never the remote_homeassistant
    // mirror_* copies of the same entities. min/max/step (0-1, step 0.05)
    // confirmed live too, not guessed.
    // 2026-09-07, direct household ask ("I need to know it works"):
    // each slider now shows what it's ACTUALLY doing this cycle, not
    // just its own setting -- solver_writer.py's own _risk_aversion_
    // effect_now() (published on this same fcEnt) exposes the real
    // raw-vs-risk-adjusted gap the LP was fed. A nonzero effect is
    // direct proof the slider bit; a genuine 0.00 at a nonzero slider
    // value is equally real proof the forecast band is currently
    // zero-width (nothing to hedge against right now), not a broken
    // control -- both states are shown explicitly rather than the
    // dashboard staying silent on which one is true.
    const riskEffects = (fcEnt && fcEnt.attributes) || {};
    const riskSliders = [
      ['number.nimbus_solver_risk_aversion', 'Load / Solar Risk Aversion', riskEffects.solar_risk_effect_now_kw, 'kW'],
      ['number.nimbus_solver_import_price_risk_aversion', 'Import Price Risk Aversion', riskEffects.import_price_risk_effect_now, '$/kWh'],
      ['number.nimbus_solver_export_price_risk_aversion', 'Export Price Risk Aversion', riskEffects.export_price_risk_effect_now, '$/kWh']
    ].map(([eid, label, effect, unit]) => {
      const ent = hass.states[eid];
      const val = ent ? parseFloat(ent.state) : NaN;
      const known = !isNaN(val);
      const pct = known ? clamp(val, 0, 1) * 100 : 0;
      const hasEffect = typeof effect === 'number';
      const effectStr = hasEffect
        ? (unit === 'kW' ? effect.toFixed(2) + ' kW' : (effect >= 0 ? '+' : '') + (effect * 100).toFixed(2) + 'c/kWh')
        : 'no data yet';
      const effectColor = hasEffect && Math.abs(effect) > 1e-6 ? '#3ddc84' : '#9aa0ac';
      return '<div class="risk-item' + (known ? '' : ' risk-item-unknown') + '">' +
        '<div class="risk-item-head"><span class="label">' + label + '</span><span class="value" data-risk-value="' + eid + '">' + (known ? val.toFixed(2) : '—') + '</span></div>' +
        '<input class="risk-slider" type="range" min="0" max="1" step="0.05" value="' + (known ? val : 0) + '" data-entity="' + eid + '" ' + (known ? '' : 'disabled') + ' style="--risk-pct:' + pct + '%">' +
        '<div class="risk-effect" style="color:' + effectColor + '">live effect this cycle: ' + effectStr + '</div>' +
      '</div>';
    }).join('');

    const tuningStats = [
      ['Min SoC', this._fmtNum(this._num('number.nimbus_solver_battery_min_soc_percent', NaN), 0, '%')],
      ['Max SoC', this._fmtNum(this._num('number.nimbus_solver_battery_max_soc_percent', NaN), 0, '%')],
      ['Capacity', this._fmtNum(this._num('number.nimbus_solver_battery_capacity_kwh', NaN), 1, ' kWh')],
      ['Efficiency', this._fmtNum(this._num('number.nimbus_solver_efficiency_percent', NaN), 0, '%')],
      ['Max Charge', this._fmtNum(this._num('number.nimbus_solver_max_charge_kw', NaN), 1, ' kW')],
      ['Max Discharge', this._fmtNum(this._num('number.nimbus_solver_max_discharge_kw', NaN), 1, ' kW')],
      ['Charge Cost', this._fmtNum(this._num('number.nimbus_solver_charge_cost', NaN), 3, '/kWh')],
      ['Discharge Cost', this._fmtNum(this._num('number.nimbus_solver_discharge_cost', NaN), 3, '/kWh')],
      ['Salvage', this._fmtNum(this._num('number.nimbus_solver_salvage_value', NaN), 2, '/kWh')]
    ];
    const tuningItems = tuningStats.map(([label, value]) =>
      '<div class="tuning-item"><span class="label">' + label + '</span><span class="value">' + value + '</span></div>'
    ).join('');

    // Economics & Quality (2026-09-05, direct household ask: "we cannot
    // see the solver table otherwise without swapping to another view").
    // Pulled from the same sensors the Solver tab's own markdown card
    // reads (sensor.nimbus_solver_battery_forecast's own attributes,
    // sensor.nimbus_solver_quality_report, sensor.p2p_nightly_volume_
    // threshold_kwh) but rendered in this card's own dark tuning-item
    // style, not a bolted-on plain table -- EPR is yesterday's real
    // retrospective score, the P2P threshold is tonight's forward
    // estimate; kept clearly labeled as such rather than implied to be
    // the same kind of number.
    const qrEnt = hass.states['sensor.nimbus_solver_quality_report'];
    const thEnt = this._p2pThresholdEntity ? hass.states[this._p2pThresholdEntity] : undefined;
    const qrOk = qrEnt && !['unavailable', 'unknown', ''].includes(qrEnt.state);
    const thOk = thEnt && !['unavailable', 'unknown', ''].includes(thEnt.state);
    const totalCost = fcEnt ? fcEnt.attributes.total_cost : undefined;
    const p2pMatchFrac = fcEnt ? fcEnt.attributes.p2p_match_fraction : undefined;
    const econStats = [
      // nimbus issue #411 (Mark Purcell): `totalCost !== undefined` only
      // catches the undefined case -- a genuinely infeasible plan's real
      // `total_cost: null` (see #390) passed this guard (`null !==
      // undefined` is true), then parseFloat(null) is NaN and
      // NaN.toFixed(2) renders the literal string "$NaN". `!= null`
      // (loose equality) catches both null and undefined in one check.
      ['Plan Cost (horizon)', totalCost != null ? '$' + parseFloat(totalCost).toFixed(2) : '—'],
      ['P2P Match', p2pMatchFrac !== undefined ? (parseFloat(p2pMatchFrac) * 100).toFixed(0) + '%' : '—'],
      // nimbus issue #411 (Mark Purcell): sensor.nimbus_solver_quality_
      // report's own `state` is already expressed as a percent (e.g.
      // "-5.76" meaning -5.76%, matching the Regret card's own "Yesterday"
      // tile) -- the `* 100` here was a real double-conversion bug,
      // rendering -576% instead of -5.8%. toFixed(1) (not toFixed(0))
      // to keep the same one-decimal precision the Regret card shows.
      ['EPR (yesterday)', qrOk ? parseFloat(qrEnt.state).toFixed(1) + '%' : '—'],
      ['Real P2P (yesterday)', qrOk ? '$' + parseFloat(qrEnt.attributes.real_p2p_dollars || 0).toFixed(2) + ' / ' + parseFloat(qrEnt.attributes.real_p2p_volume_kwh || 0).toFixed(1) + 'kWh' : '—'],
      ['Tracking Fidelity', qrOk ? (parseFloat(qrEnt.attributes.tracking_fidelity || 0) * 100).toFixed(0) + '%' : '—']
    ];
    if (this._p2pThresholdEntity) {
      econStats.push(["Tonight's P2P Threshold", thOk ? parseFloat(thEnt.state).toFixed(1) + ' kWh' : '—']);
    }
    const econItems = econStats.map(([label, value]) =>
      '<div class="tuning-item"><span class="label">' + label + '</span><span class="value">' + value + '</span></div>'
    ).join('');

    // Forecast interval table (2026-09-05). Direct household correction:
    // "make sure the table in control panel has the same slots as
    // markdown table with the same match and breakdowns... just applying
    // your new styling" + "it has to have cost, fees, fit, p2p etc." --
    // this now mirrors devhub's own real "Solver Forecast" markdown card
    // (nimbus-devhub, Forecaster view) EXACTLY: same 10 columns, same
    // math, same TOTAL-row-then-stop-at-midnight behaviour, same P2P
    // field semantics -- only the rendering is this card's own dark
    // styling. Column math, verbatim from that markdown card's own Jinja:
    //   Buy¢  = import_price_raw * 100         (raw commodity price only)
    //   Fees¢ = (import_price - import_price_raw) * 100   (TOU/certs on top)
    //   Sell¢ = export_price * 100             (plain spot, on its own)
    //   P2P¢  = (export_price + bonus_price) * 100 when bonus_price > 0.01,
    //           else 0 -- the real, standalone P2P settlement rate, never
    //           bonus_price bare and never blended into Sell¢.
    //   Batt  = battery_kw, plus a lightning-bolt suffix when
    //           export_bonus_kw > 0 (this period is genuinely counting
    //           toward the P2P-matched volume).
    //   Grid  = energy balance across the same three sources used
    //           everywhere on this card (grid + solar + discharge = load +
    //           charge + export) -- positive imports, negative exports.
    // Runs from "now" until the forecast's own date rolls over, then
    // renders one bold TOTAL row (today's real P2P $ + Net$ across every
    // period shown) and stops -- identical scope to the markdown card,
    // not an arbitrary row cap.
    const ftFmtTime = (t) => {
      const d = new Date(t);
      return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    };
    const ftDateKey = (t) => {
      const d = new Date(t);
      return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    };
    let ftDate = null, ftDone = false, ftDayNet = 0, ftDayP2p = 0, ftPeriodCount = 0;
    const ftRows = [];
    // Real, live grid reading -- used to prefer measured data over the
    // forecast estimate specifically on the "now" row (direct household
    // ask: "maybe at current time period row?"). Every other row keeps the
    // Solver's own forecast-derived figure, since nothing measured exists
    // for a future period.
    const realGridNow = this._num(this._gridEntity, NaN);
    // nimbus issue #421 (Mark Purcell): the identical real-data-on-the-
    // NOW-row treatment above was never applied to the BATT column --
    // it always showed the Solver's own PLANNED battery_kw, even for
    // the current period, while the big status heading above (and the
    // reasoning text) show the REAL measured, sign-corrected reading.
    // With dispatch disarmed, the plan is a genuine hypothetical that
    // can legitimately disagree with reality in both direction and
    // magnitude -- confirmed live: heading showed "CHARGING (SOLAR)"/
    // -7.0kW while this same row's own BATT cell showed +1.7kW
    // (discharging, by this card's own sign convention) for the exact
    // same instant. Same fix as realGridNow: prefer the real, sign-
    // corrected measured value on the now row only.
    const realBattNow = this._num(this._battEntity, NaN) * this._battSign();
    for (let idx = 0; idx < fc.length && !ftDone; idx++) {
      const p = fc[idx];
      const pDate = ftDateKey(p.time);
      if (ftDate !== null && pDate !== ftDate) {
        const totalNetClass = ftDayNet < -0.001 ? 'net-pos' : (ftDayNet > 0.001 ? 'net-neg' : '');
        ftRows.push(
          // colspan="2" (Time+Source): without this, this row's own
          // long label was the REAL reason the Time column read so wide
          // in every other row too -- table auto-layout sizes a column
          // by its widest cell across every row, so one long label here
          // forced column 1 wide regardless of how short "14:30" is
          // everywhere else. Spanning it across two columns instead
          // lets Time shrink to what its own real content needs.
          '<tr class="total-row">' +
            '<td colspan="2" style="font-weight:700;">&mdash; ' + ftDate + ' TOTAL (shown rows) &mdash;</td>' +
            '<td class="num"></td><td class="num"></td><td class="num"></td>' +
            '<td class="num"><span class="p2p-pill">+$' + ftDayP2p.toFixed(2) + '</span></td>' +
            '<td class="num"></td><td class="num"></td><td class="num"></td><td class="num"></td><td class="num"></td>' +
            '<td class="num ' + totalNetClass + '" style="font-weight:700;">$' + ftDayNet.toFixed(2) + '</td>' +
          '</tr>'
        );
        ftDone = true;
        break;
      }
      ftDate = pDate;
      ftPeriodCount++;
      ftDayNet += parseFloat(p.net_cost) || 0;
      ftDayP2p += (parseFloat(p.bonus_price) || 0) * (parseFloat(p.export_bonus_kw) || 0) * (parseFloat(p.hours) || 0);

      const buyC = (parseFloat(p.import_price_raw) || 0) * 100;
      const feesC = ((parseFloat(p.import_price) || 0) * 100) - buyC;
      const spotC = (parseFloat(p.export_price) || 0) * 100;
      const bonusRaw = parseFloat(p.bonus_price) || 0;
      const p2pActive = bonusRaw > 0.01;
      const p2pFullC = p2pActive ? spotC + bonusRaw * 100 : 0;
      let bkwRow = parseFloat(p.battery_kw) || 0;
      if (idx === 0 && !isNaN(realBattNow)) bkwRow = realBattNow;
      const battColor = bkwRow > 0.05 ? '#3ddc84' : (bkwRow < -0.05 ? '#4fa3ff' : '#5a6070');
      const bonusKwActive = (parseFloat(p.export_bonus_kw) || 0) > 0;
      const net = parseFloat(p.net_cost) || 0;
      const netClass = net < -0.001 ? 'net-pos' : (net > 0.001 ? 'net-neg' : '');
      const p2pCell = p2pActive
        ? '<span class="p2p-pill">' + p2pFullC.toFixed(1) + '</span>'
        : '<span style="opacity:0.35;">0.0</span>';
      // Source split (Solar vs Grid share of this period's dispatch) --
      // direct household ask to bring this back: "i loved the small bar
      // with yellow and blue percentages... place it between Time and
      // Buy". Real per-period fields already published by the Solver
      // (dispatch_source_a/b_label/_pct) -- not invented, same fields
      // this card used before the markdown-table-parity pass removed it.
      const srcAPct = parseFloat(p.dispatch_source_a_pct);
      const srcBPct = parseFloat(p.dispatch_source_b_pct);
      const hasSource = !isNaN(srcAPct) && !isNaN(srcBPct) && (srcAPct + srcBPct) > 0;
      const sourceBar = hasSource
        ? '<div class="source-bar" title="' + (p.dispatch_source_a_label || 'Solar') + ' ' + srcAPct.toFixed(0) + '% / ' + (p.dispatch_source_b_label || 'Grid') + ' ' + srcBPct.toFixed(0) + '%">' +
            '<span class="seg seg-a" style="width:' + srcAPct + '%"></span>' +
            '<span class="seg seg-b" style="width:' + srcBPct + '%"></span>' +
          '</div>'
        : '<span style="opacity:0.3;">—</span>';
      // Grid column (direct household ask): energy balance across the same
      // three sources used everywhere else on this card -- grid + solar +
      // discharge = load + charge + export, rearranged to isolate grid.
      // Positive = importing from the grid, negative = exporting to it.
      // On the "now" row specifically, prefer the real measured meter
      // reading over this forecast-derived figure when it's available.
      const loadKwRow = parseFloat(p.load_kw) || 0;
      const solarKwRow = parseFloat(p.solar_kw) || 0;
      const dischargeKwRow = bkwRow > 0 ? bkwRow : 0;
      const chargeKwRow = bkwRow < 0 ? -bkwRow : 0;
      let gridKwRow = loadKwRow + chargeKwRow - solarKwRow - dischargeKwRow;
      if (idx === 0 && !isNaN(realGridNow)) gridKwRow = realGridNow;
      const gridColor = gridKwRow > 0.05 ? '#ffb340' : (gridKwRow < -0.05 ? '#3ddc84' : '#5a6070');
      ftRows.push(
        '<tr class="' + (idx === 0 ? 'now-row' : '') + '">' +
          '<td class="time-col">' + ftFmtTime(p.time) + (idx === 0 ? ' <span class="now-tag">now</span>' : '') + '</td>' +
          '<td class="source-col">' + sourceBar + '</td>' +
          '<td class="num">' + buyC.toFixed(1) + '</td>' +
          '<td class="num">' + feesC.toFixed(1) + '</td>' +
          '<td class="num">' + spotC.toFixed(1) + '</td>' +
          '<td class="num">' + p2pCell + '</td>' +
          '<td class="num">' + (parseFloat(p.load_kw) || 0).toFixed(1) + '</td>' +
          '<td class="num">' + (parseFloat(p.solar_kw) || 0).toFixed(1) + '</td>' +
          '<td class="num" style="color:' + battColor + '; font-weight:600;">' + bkwRow.toFixed(1) + (bonusKwActive ? ' &#9889;' : '') + '</td>' +
          '<td class="num" style="color:' + gridColor + ';">' + gridKwRow.toFixed(1) + '</td>' +
          '<td class="num">' + (parseFloat(p.soc_pct) || 0).toFixed(0) + '%</td>' +
          '<td class="num ' + netClass + '">$' + net.toFixed(2) + '</td>' +
        '</tr>'
      );
    }
    const forecastRows = ftRows.join('');
    const FT_ROWS = ftPeriodCount;

    if (!this._built) { this.attachShadow({mode: 'open'}); this._built = true; }

    this.shadowRoot.innerHTML =
      '<style>' +
        /* nimbus issue #400 (Mark Purcell): the right column's grid-track
           minimum (below) and table.ftable's own real min-width used to be
           two independently-hardcoded numbers (460px vs 640px) that
           silently drifted apart -- the actual root cause of #400, not
           just "the wrong number." A single custom property is now the
           one real source of truth for "how wide the forecast table
           actually needs to be"; both the table's own min-width and the
           grid track that has to make room for it reference this SAME
           value, so a future change to the table (an added column, wider
           currency formatting) can never again silently reintroduce this
           exact mismatch in only one of the two places. */
        // 2026-09-07: reduced 640px -> 560px -- 640 was set (issue #400)
        // before #457's own row-padding tightening (7px/12px -> 3px/10px)
        // and this session's SOURCE-column shrink (34px -> 22px bar,
        // tighter padding); 640 had gone stale and was forcing the table
        // wider than its real current content needs, causing an
        // unwanted horizontal scrollbar in the new chart/table
        // side-by-side layout below. Re-verify this value directly
        // (not by feel) if row content changes again.
        ':host { display:block; container-type: inline-size; --ftable-min-width: 560px; }' +
        // nimbus issue #400 follow-up: a screenshot from Mark's real
        // Sections-view dashboard showed forecast-table content stopping
        // hard at the card's own right edge with no visible scrollbar --
        // not what .ftable-wrap's own overflow-x:auto should produce if
        // it were genuinely containing the overflow. .card itself had no
        // overflow containment of its own, meaning IF anything upstream
        // (grid track sizing, an unusual Sections-view width computation)
        // ever renders a descendant wider than intended, that overflow
        // has nothing stopping it from visually bleeding past the card's
        // own rounded border into the surrounding dashboard, instead of
        // being contained and left to whichever inner element's own
        // overflow-x:auto SHOULD be handling it. This is a real, always-
        // worth-having defensive backstop regardless of whether it's the
        // exact mechanism in Mark's case (still being confirmed against
        // his real rendered width) -- converts "content bleeds past the
        // card into the dashboard" into, at worst, "content is clipped at
        // the card's own edge," never a broken-looking layout escape.
        '.card { background: radial-gradient(circle at 15% 0%, #1c2433 0%, #0f131b 60%), linear-gradient(160deg, #14181f 0%, #0d1016 100%);' +
          ' border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; padding: 26px 30px 24px; color: #e8eaf0;' +
          ' font-family: var(--paper-font-body1_-_font-family, sans-serif); box-shadow: 0 8px 32px rgba(0,0,0,0.45); overflow-x: hidden;}' +
        '.top-row { display:flex; align-items:flex-start; justify-content:space-between; gap: 24px; flex-wrap: wrap; }' +
        '.title-row { display:flex; align-items:center; gap: 18px; flex-wrap: wrap; }' +
        '.title { font-size: 1.5em; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; opacity: 0.95; }' +
        '.subtitle { font-size: 1.15em; opacity: 0.5; margin-top: 4px; }' +
        '.mode-pill { font-size: 1.0em; padding: 6px 14px; border-radius: 20px; font-weight:700; letter-spacing:0.06em;' +
          ' background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); white-space: nowrap; }' +
        '.mode-chip-row { display:flex; gap: 6px; flex-wrap: wrap; }' +
        '.mode-chip { background: rgba(255,255,255,0.03); border: 1.5px solid rgba(255,255,255,0.1); border-radius: 10px;' +
          ' padding: 6px 11px; color: #cfd3dc; font-size: 0.95em; font-weight: 700; letter-spacing: 0.03em;' +
          ' cursor: pointer; transition: all 0.2s ease; }' +
        '.mode-chip:hover { background: rgba(255,255,255,0.08); border-color: rgba(255,255,255,0.22); }' +
        '.mode-chip.active { background: rgba(79,163,255,0.18); border-color: #4fa3ff; color: #fff; box-shadow: 0 0 10px rgba(79,163,255,0.3); }' +
        '.mode-chip[data-mode="Automatic"].active { background: rgba(61,220,132,0.18); border-color: #3ddc84; box-shadow: 0 0 10px rgba(61,220,132,0.35); }' +
        '.kill-switch { display:flex; align-items:center; gap: 10px; cursor: pointer; user-select: none; flex-shrink: 0; }' +
        '.kill-switch .label { font-size: 1.1em; font-weight: 700; letter-spacing: 0.05em; }' +
        '.kill-switch .track { width: 56px; height: 30px; border-radius: 15px; position: relative; transition: background 0.2s ease;' +
          ' border: 1.5px solid rgba(255,255,255,0.15); }' +
        '.kill-switch .knob { width: 24px; height: 24px; border-radius: 50%; background: #fff; position: absolute; top: 2px; transition: left 0.2s ease;' +
          ' box-shadow: 0 1px 4px rgba(0,0,0,0.4); }' +
        '.hero { display:flex; align-items:center; gap: 40px; margin: 16px 0 8px; flex-wrap: wrap; width: 100%; }' +
        '.hero svg { width: 100%; max-width: 300px; height: auto; flex-shrink: 0; }' +
        '.gauge-status { text-align:left; flex-shrink: 0; max-width: 260px; }' +
        '.gauge-status .dir { font-size: 1.7em; font-weight: 800; letter-spacing: 0.02em; line-height: 1.15; }' +
        '.gauge-status .kw { font-size: 1.5em; opacity: 0.75; font-weight: 500; margin-top: 2px; }' +
        '.kw-triple { display:flex; flex-direction:column; gap: 2px; font-size: 1.05em; opacity: 0.85; margin-top: 4px; }' +
        '.kw-triple b { opacity: 0.55; font-weight: 600; margin-right: 4px; }' +
        '.reasoning { font-size: 1.35em; line-height: 1.5; opacity: 0.85; flex: 1 1 300px; min-width: 260px; }' +
        '.range-labels { display:flex; justify-content:space-between; font-size: 1.05em; opacity: 0.5; width: 250px; margin-top: 4px; }' +
        '.section-label { font-size: 1.05em; text-transform: uppercase; letter-spacing: 0.09em; opacity: 0.5; margin: 22px 0 8px; }' +
        // 2026-09-07, direct household instruction after four failed
        // content-aware formulas (fixed 2fr/1fr + breakpoint, flexbox,
        // grid+minmax(max-content,1fr), grid+"1fr max-content"): design
        // the CONTAINERS first, fixed at exactly 2/3 and 1/3 of the
        // row, and place each object inside its own box -- rather than
        // trying to make either box grow or shrink to match its
        // content, which is what caused every prior failure (whichever
        // box's content was estimated wrong ended up crushing the
        // other, or overflowing the row).
        //
        // grid-template-columns: 2fr 1fr is a literal, content-BLIND
        // fixed split -- chart-col is always exactly 2/3 of the row,
        // table-col always exactly 1/3, regardless of what's inside
        // either one. `min-width: 0` on both (overriding a CSS Grid
        // item's own default auto-min-size, which is otherwise its
        // content's max-content width and WOULD silently override the
        // fr split for whichever side has more content) is what makes
        // this actually hold: without it, table-col's real content
        // would still force it wider than 1/3 the moment that content
        // exceeds a third of the row, the exact failure mode from every
        // earlier attempt tonight. With it, table-col is a true, fixed
        // 1/3-width box; if table.ftable's real content is wider than
        // that box, .ftable-wrap's own overflow-x:auto scrolls WITHIN
        // that box only -- it can never crush the chart or push the
        // page/card wider. Same real tradeoff as the very first
        // attempt tonight (a fixed fraction can mean an internal table
        // scrollbar on a content-heavy render), but now bounded and
        // contained instead of distorting the whole row's geometry.
        '.chart-table-grid { display:block; }' +
        '.chart-col, .table-col { min-width: 0; }' +
        '@container (min-width: 900px) {' +
          '.chart-table-grid { display:grid; grid-template-columns: 2fr 1fr; gap: 20px 28px; align-items:stretch; }' +
        '}' +
        // Direct household ask (2026-09-07): chart should stretch down
        // to fill whatever vertical space table-col's own (taller) row
        // count gives it, not sit at a fixed aspect-ratio height with
        // empty space below. align-items:stretch above makes chart-col
        // itself match table-col's height; chart-col is flex-column so
        // .timeline-wrap can flex:1 to fill THAT; the svg's own
        // height:100% (was auto) plus preserveAspectRatio="none" (set
        // where the svg is built, not here) is what actually stretches
        // the drawn chart to fill the taller box instead of
        // letterboxing at its own fixed 1000:275 ratio.
        '.chart-col { display:flex; flex-direction:column; }' +
        '.timeline-wrap { width: 100%; flex: 1; min-height: 0; overflow-x: auto; }' +
        '.timeline-wrap svg { width: 100%; height: 100%; display: block; min-width: 480px; }' +
        '.legend { display:flex; gap: 20px; font-size: 1.05em; opacity: 0.65; margin-top: 8px; flex-wrap: wrap; }' +
        '.legend span { display:inline-flex; align-items:center; gap:6px; }' +
        '.legend .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }' +
        '.footer { display:flex; gap: 24px; flex-wrap: wrap; margin-top: 20px; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.06);' +
          ' font-size: 1.15em; opacity: 0.78; align-items: center; }' +
        '.chip { display:inline-flex; align-items:center; gap:7px; }' +
        '.chip .dot { width:9px; height:9px; border-radius:50%; display:inline-block; }' +
        '.solve-now-btn { margin-left: auto; background: rgba(79,163,255,0.14); border: 1.5px solid rgba(79,163,255,0.4); color: #cfe4ff;' +
          ' border-radius: 8px; padding: 6px 14px; font-size: 0.9em; font-weight: 700; letter-spacing: 0.02em; cursor: pointer; transition: background 0.15s ease; }' +
        '.solve-now-btn:hover:not(:disabled) { background: rgba(79,163,255,0.26); }' +
        '.solve-now-btn:disabled { opacity: 0.6; cursor: not-allowed; }' +
        '.tuning-row { display:flex; gap: 28px; flex-wrap: wrap; margin-top: 18px; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.06); align-items: center; }' +
        '.tuning-item { display:flex; flex-direction:column; gap: 3px; }' +
        '.tuning-item .label { font-size: 1.0em; text-transform: uppercase; letter-spacing: 0.07em; opacity: 0.45; }' +
        '.tuning-item .value { font-size: 1.4em; font-weight: 600; }' +
        '.tuning-link { margin-left: auto; font-size: 1.15em; color: #4fa3ff; text-decoration: none; align-self: center; }' +
        '.tuning-link:hover { text-decoration: underline; }' +
        '.risk-row { display:flex; gap: 28px; flex-wrap: wrap; margin: 4px 0 18px; }' +
        '.risk-item { display:flex; flex-direction:column; gap: 6px; min-width: 200px; flex: 1 1 220px; }' +
        '.risk-item-unknown { opacity: 0.4; }' +
        '.risk-item-head { display:flex; justify-content:space-between; align-items:baseline; font-size: 1.1em; }' +
        '.risk-item-head .label { text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.82em; opacity: 0.55; }' +
        '.risk-item-head .value { font-weight: 700; font-size: 1.15em; color: #4fa3ff; }' +
        '.risk-slider { -webkit-appearance: none; appearance: none; width: 100%; height: 6px; border-radius: 3px; cursor: pointer;' +
          ' background: linear-gradient(90deg, #4fa3ff var(--risk-pct, 0%), rgba(255,255,255,0.08) var(--risk-pct, 0%)); outline: none; }' +
        '.risk-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 18px; height: 18px; border-radius: 50%; background: #fff;' +
          ' border: 3px solid #4fa3ff; cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,0.5); }' +
        '.risk-slider::-moz-range-thumb { width: 18px; height: 18px; border-radius: 50%; background: #fff; border: 3px solid #4fa3ff; cursor: pointer; }' +
        '.risk-slider:disabled { cursor: not-allowed; }' +
        '.risk-effect { font-size: 0.85em; opacity: 0.75; font-variant-numeric: tabular-nums; }' +
        // nimbus issue #456 (Mark Purcell): only ~11 of 142 real periods
        // were visible per screenful -- max-height raised (more rows fit
        // before scrolling) and td padding tightened (each row itself
        // takes less vertical space) together, not just one or the other,
        // since either alone still leaves most of a 142-row table below
        // the fold.
        '.ftable-wrap { width: 100%; overflow-x: auto; overflow-y: auto; max-height: 640px; margin-top: 4px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.06); }' +
        '.ftable-note { font-size: 1.0em; opacity: 0.45; margin: 0 0 6px; }' +
        // nimbus issue #459 (Mark Purcell) removed width:100% here because
        // back then table.ftable's container was the WHOLE wide panel
        // (uncapped, ~1900px) -- leftover width past the table's real
        // content got redistributed as ugly empty column padding.
        // 2026-09-07: re-added now that the container is different --
        // table-col is a fixed, content-blind 1/3 of the row (see
        // .chart-table-grid's own CSS comment), a modest, STABLE box, not
        // an unbounded panel. Without width:100%, table.ftable sat at its
        // own natural content width inside that box, which on a lighter
        // render (fewer P2P pills, fewer populated columns) left real dead
        // space on both sides while every column stayed packed at its bare
        // minimum -- squished AND wasted space at once, a direct household
        // report. With width:100%, the table always exactly fills its
        // fixed box: on a lighter render the extra width spreads evenly
        // across columns instead of sitting empty; on a heavier render
        // (more real content than the box can hold) min-width plus
        // .ftable-wrap's own overflow-x:auto still floor it and scroll
        // internally, unchanged -- the #459 regression can't recur because
        // the container itself can no longer grow unbounded the way the
        // old full-width single-column layout could.
        'table.ftable { border-collapse: collapse; font-size: 1.08em; min-width: var(--ftable-min-width); width: 100%; }' +
        'table.ftable thead th { text-align: left; text-transform: uppercase; letter-spacing: 0.06em; font-size: 0.85em; opacity: 0.5;' +
          ' font-weight: 600; padding: 6px 10px; border-bottom: 1px solid rgba(255,255,255,0.1); position: sticky; top: 0; background: #14181f; }' +
        'table.ftable thead th.num { text-align: right; }' +
        // nimbus issue #456: 7px 12px sized each row for something denser
        // than one line of 1-2 digit numbers -- 3px 10px keeps every
        // value comfortably legible while roughly halving row height, and
        // .num gets an even tighter 6px horizontal since a short number
        // needs far less side margin than a text label does.
        'table.ftable td { padding: 3px 10px; border-bottom: 1px solid rgba(255,255,255,0.04); white-space: nowrap; }' +
        'table.ftable td.num { text-align: right; font-variant-numeric: tabular-nums; padding-left: 6px; padding-right: 6px; }' +
        // Direct household ask: Time read as a "huge wide column" --
        // real cause was the total-row's own long label sharing this
        // column (fixed above via colspan), not the short "14:30 now"
        // values themselves. This padding tightens it further now that
        // nothing else is forcing it wide.
        'table.ftable th.time-col, table.ftable td.time-col { padding-left: 8px; padding-right: 8px; }' +
        // Direct household ask (2026-09-07): narrower still, but bar
        // width and font must NOT change -- padding is the only
        // remaining knob.
        'table.ftable th.source-col, table.ftable td.source-col { padding-left: 2px; padding-right: 2px; }' +
        // "Source" (uppercase + 0.06em letter-spacing, same as every
        // other header) is itself wider than the 14px bar below it --
        // dropping letter-spacing and shrinking the font specifically
        // for this one header keeps the real word instead of
        // abbreviating it, while still actually fitting tight.
        'table.ftable th.source-col { letter-spacing: 0; font-size: 0.72em; }' +
        'table.ftable tbody tr:nth-child(even) { background: rgba(255,255,255,0.02); }' +
        'table.ftable tbody tr:hover { background: rgba(79,163,255,0.08); }' +
        'table.ftable td.net-pos { color: #3ddc84; }' +
        'table.ftable td.net-neg { color: #ff7a7a; }' +
        'table.ftable tr.now-row td { background: rgba(255,77,141,0.07); border-bottom-color: rgba(255,77,141,0.15); }' +
        'table.ftable tr.now-row td:first-child { border-left: 2px solid #ff4d8d; }' +
        'table.ftable tr.total-row td { background: rgba(255,213,79,0.06); border-top: 1px solid rgba(255,213,79,0.25); border-bottom: 1px solid rgba(255,213,79,0.25); }' +
        '.now-tag { font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.06em; color: #ff4d8d; opacity: 0.85; margin-left: 4px; }' +
        '.p2p-pill { display: inline-block; background: rgba(255,213,79,0.16); color: #ffd54f; border: 1px solid rgba(255,213,79,0.35);' +
          ' border-radius: 10px; padding: 1px 9px; font-weight: 700; font-size: 0.92em; }' +
        // nimbus issue #456: 56px gave the SOURCE indicator noticeably
        // more visual weight than the numeric columns beside it -- 34px
        // still reads clearly as a two-segment bar (its own title
        // attribute carries the exact solar/grid % on hover) at a width
        // closer to what a numeric column actually needs.
        // Direct household ask (2026-09-07): table needed a horizontal
        // scrollbar in the new 2/3+1/3 layout -- SOURCE was still sized
        // for the old wider row padding, not #457's tightened one.
        // 34px -> 22px plus tighter column padding (matches .num's own
        // 6px, was the default 10px td padding) directly reduces the
        // table's real minimum content width instead of growing the
        // grid share to compensate.
        // 2026-09-07: 22px still wasn't narrow enough -- confirmed live,
        // still scrolling. 14px, no more shrinking room without losing
        // the two-segment bar shape entirely.
        '.source-bar { display: flex; width: 14px; height: 7px; border-radius: 3px; overflow: hidden; background: rgba(255,255,255,0.06); }' +
        '.source-bar .seg-a { background: #ffb340; }' +
        '.source-bar .seg-b { background: #4fa3ff; }' +
        /* Landscape/desktop layout (nimbus issue #391, Mark Purcell): below
           this breakpoint the card stays a single vertical column (the
           original, unchanged mobile/narrow layout). At/above it, the header
           + hero gauge + risk sliders move into a left column alongside the
           timeline + forecast table in a right column, so a wide desktop
           panel view uses the available horizontal space instead of one
           long scroll. Footer/tuning/economics stay full-width below both
           columns either way -- they're already horizontal chip rows, not
           something that benefits from a second column.

           FIXED 2026-09-06 (regression, Mark Purcell): the first version of
           this used `@media (min-width: 900px)`, which measures the BROWSER
           VIEWPORT width, not the card's own rendered width. In a real HA
           dashboard the card sits behind the sidebar/header chrome and any
           dashboard-level column/section sizing -- its own actual available
           width can be well under the full viewport even when the viewport
           itself is plenty wide, so the media query can silently never fire
           depending on window size, sidebar state, or how the dashboard
           view lays this card out, with zero connection to whether the CARD
           ITSELF has room for two columns. Switched to a CSS container
           query instead (`container-type: inline-size` on :host, `@container`
           instead of `@media`) -- this measures the card's own rendered
           width directly, so the two-column layout applies exactly when
           there's genuinely enough room for it, independent of viewport
           size, sidebar, or how any specific dashboard chooses to size this
           card's own container.

           FIXED 2026-09-06 (issue #400, Mark Purcell): the container
           query fired correctly (confirmed live, this fix's own
           container-type/@container are what's being served), but its
           900px breakpoint and the right column's 460px track minimum
           were never reconciled against what that column's own children
           actually need -- `.timeline-wrap svg` and `table.ftable` below
           both carry a real `min-width` of their own (480px/640px), and
           `.col-right { min-width: 0 }` (needed so the grid TRACK itself
           can't force the whole card wider, the mechanism that fixed
           #391) means once the track ends up narrower than 640px, the
           table just overflows/scrolls inside its own `.ftable-wrap`
           instead. Two columns firing on a card that's, say, 950px wide
           gives the right column roughly 460-500px -- comfortably over
           the OLD 460px floor, comfortably under the table's real 640px
           need -- so Mark saw exactly the "fires, but doesn't fit"
           overflow his own issue described, not #391's original
           "never fires at all" failure.

           Fixed at the root, not just with new numbers: two independently
           hardcoded numbers drifting apart is exactly what caused this,
           so patching in another pair of literals that happen to agree
           TODAY would only set up the identical bug for whenever
           table.ftable's own real min-width next changes (an added
           column, wider currency formatting). Instead, the right
           column's grid-track minimum below now reads
           `var(--ftable-min-width)` -- the SAME custom property
           `table.ftable`'s own `min-width` uses (declared once on
           `:host`, above) -- so the two values structurally cannot
           disagree again; change the table's real minimum and the grid
           track that has to make room for it updates automatically.
           (The breakpoint itself has to stay a literal -- `@container`
           size-query conditions don't reliably support `var()` across
           every frontend HA embeds, unlike a plain property value like
           the grid-template-columns line below -- so 1020px is derived
           by hand: 320 (left floor) + 36 (gap) + 640 (--ftable-min-width,
           kept in sync by eye since a query condition can't reference it
           directly) + a 24px safety margin for scrollbar/border
           rounding. If --ftable-min-width above is ever changed, this
           breakpoint needs the same arithmetic redone by hand.)
           Two columns now only ever appear once there's genuinely enough
           real room for both at their own true minimum widths, with zero
           gap between "fires" and "fits". `1.65fr` is unchanged, so on
           any screen wider than the breakpoint the right column still
           grows well past its floor, taking its normal proportional
           share of whatever space is left over -- the floor is only
           ever a minimum, never a ceiling. */
        // 2026-09-07 (household explicit request): the landscape/2-column
        // mode above disliked in practice regardless of available width --
        // reverting to the original single-column stacked layout unconditionally.
        // Deliberately NOT deleting the @container plumbing above
        // (container-type/--ftable-min-width) since nothing else in this
        // file depends on removing it, and it's a smaller diff to just
        // never trigger the grid than to unwind everything that grew
        // around it (#391/#395/#400/#407).
        '.layout-grid { display:block; }' +
      '</style>' +
      '<div class="card">' +
      '<div class="layout-grid">' +
      '<div class="col-left">' +
        '<div class="top-row">' +
          '<div class="title-row">' +
            '<div><div class="title">Nimbus Dispatch</div></div>' +
          '</div>' +
          '<div style="display:flex; align-items:center; gap:16px; flex-wrap: wrap;">' +
            '<div class="mode-chip-row">' + modeButtons + '</div>' +
            '<div class="mode-pill" style="color:' + dirColor + '">' + (armed ? 'ARMED' : 'DISARMED') + '</div>' +
            '<div class="kill-switch" id="kill-switch">' +
              '<span class="label" style="color:' + (armed ? '#5a6070' : '#e8eaf0') + '">OFF</span>' +
              '<div class="track" style="background:' + (armed ? 'rgba(61,220,132,0.35)' : 'rgba(255,255,255,0.08)') + '; border-color:' + (armed ? '#3ddc84' : 'rgba(255,255,255,0.15)') + '">' +
                '<div class="knob" style="left:' + (armed ? '28px' : '3px') + '; background:' + (armed ? '#3ddc84' : '#fff') + '"></div>' +
              '</div>' +
              '<span class="label" style="color:' + (armed ? '#3ddc84' : '#5a6070') + '">ON</span>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="hero">' +
          '<svg viewBox="0 0 250 145" preserveAspectRatio="xMidYMid meet">' +
            gaugeBands +
            '<circle cx="' + needleX + '" cy="' + needleY + '" r="8" fill="#fff" stroke="' + gaugeColor + '" stroke-width="3"/>' +
            '<text x="125" y="118" text-anchor="middle" fill="#e8eaf0" font-size="26" font-weight="700">' + soc.toFixed(0) + '%</text>' +
          '</svg>' +
          '<div class="gauge-status">' +
            '<div class="dir" style="color:' + dirColor + '">' + dir + '</div>' +
            (kwTriple
              ? '<div class="kw-triple">' +
                  '<span><b>Grid</b> ' + kwTriple.grid.toFixed(1) + ' kW</span>' +
                  '<span><b>Solar</b> ' + kwTriple.solar.toFixed(1) + ' kW</span>' +
                  '<span><b>Battery</b> ' + kwTriple.batt.toFixed(1) + ' kW</span>' +
                '</div>'
              : '<div class="kw">' + (isIdle ? 'Idle' : Math.abs(bkw).toFixed(1) + ' kW') + '</div>') +
            '<div class="range-labels"><span>' + minSoc + '% floor</span><span>' + maxSoc + '% ceiling</span></div>' +
          '</div>' +
          '<div class="reasoning">' + reasoning + '</div>' +
        '</div>' +
        '<div class="section-label" style="margin:8px 0 6px;">Risk Sliders -- how much the plan hedges against forecast being wrong</div>' +
        '<div class="risk-row">' + riskSliders + '</div>' +
      '</div>' + // .col-left
      '<div class="col-right">' +
      // Fixed 2/3:1/3 containers -- see .chart-table-grid's own CSS
      // comment, above, for why content-aware sizing was abandoned.
      '<div class="chart-table-grid">' +
      '<div class="chart-col">' +
        '<div class="section-label" style="margin-top:0;">Dispatch Plan - next 3 days (plan vs. actual)</div>' +
        // preserveAspectRatio="none" (was xMidYMid meet) -- direct
        // household ask: stretch to fill the taller box .chart-col's
        // own align-items:stretch now gives it, rather than
        // letterboxing at the fixed 1000:TH ratio. Deliberate stretch,
        // not a bug -- this SVG has no circular/square elements whose
        // aspect ratio would look wrong distorted.
        '<div class="timeline-wrap"><svg viewBox="0 0 ' + TW + ' ' + TH + '" preserveAspectRatio="none">' +
          '<defs>' +
            '<linearGradient id="fillGradV4" x1="0" y1="0" x2="0" y2="1">' +
              '<stop offset="0%" stop-color="#3ddc84" stop-opacity="0.55"/>' +
              '<stop offset="45%" stop-color="#3ddc84" stop-opacity="0.08"/>' +
              '<stop offset="55%" stop-color="#4fa3ff" stop-opacity="0.08"/>' +
              '<stop offset="100%" stop-color="#4fa3ff" stop-opacity="0.55"/>' +
            '</linearGradient>' +
          '</defs>' +
          '<line x1="' + padL + '" y1="' + yZero + '" x2="' + (TW - padR) + '" y2="' + yZero + '" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>' +
          p2pBands +
          (areaPath ? '<path d="' + areaPath + '" fill="url(#fillGradV4)" stroke="none"/>' : '') +
          hourMarks.map(m => '<line x1="' + m.x + '" y1="' + padT + '" x2="' + m.x + '" y2="' + (padT + plotH) + '" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>' +
            '<text x="' + m.x + '" y="' + (TH - 12) + '" fill="#e8eaf0" opacity="0.5" font-size="6" text-anchor="middle">' + m.label + '</text>').join('') +
          (actualPath ? '<path d="' + actualPath + '" fill="none" stroke="#ffffff" stroke-width="1.1" stroke-opacity="0.8"/>' : '') +
          (socPath ? '<path d="' + socPath + '" fill="none" stroke="#ffb340" stroke-width="1.6" stroke-dasharray="3 3" stroke-opacity="0.8"/>' : '') +
          '<line x1="' + nowX + '" y1="' + padT + '" x2="' + nowX + '" y2="' + (padT + plotH) + '" stroke="#ff4d8d" stroke-width="1.5"/>' +
          '<circle cx="' + nowX + '" cy="' + padT + '" r="3" fill="#ff4d8d"/>' +
        '</svg></div>' +
        '<div class="legend">' +
          '<span><span class="dot" style="background:#3ddc84"></span>Planned discharge</span>' +
          '<span><span class="dot" style="background:#4fa3ff"></span>Planned charge</span>' +
          '<span><span class="dot" style="background:#ffffff"></span>Actual (measured, last 18h)</span>' +
          '<span><span class="dot" style="background:#ffb340"></span>Planned SoC %</span>' +
          '<span><span class="dot" style="background:#ff4d8d"></span>Now</span>' +
          '<span><span class="dot" style="background:#ffd54f"></span>P2P active</span>' +
        '</div>' +
      '</div>' + // .chart-col
      '<div class="table-col">' +
        '<div class="section-label" style="margin-top:0;">Forecast Intervals -- now until midnight (' + FT_ROWS + ' periods shown)</div>' +
        '<div class="ftable-note">' +
          '<span class="source-bar" style="display:inline-flex; vertical-align:middle;"><span class="seg seg-a" style="width:60%"></span><span class="seg seg-b" style="width:40%"></span></span> Source = Solar (orange) / Grid (blue) share &middot; ' +
          'Buy&cent; = raw commodity price &middot; Fees&cent; = network TOU/certificates on top &middot; P2P&cent; = real total P2P rate (Sell&cent; + bonus, gold when active, 0 otherwise) &middot; &#9889; = period counts toward tonight\'s matched P2P volume' +
        '</div>' +
        '<div class="ftable-wrap"><table class="ftable">' +
          '<thead><tr>' +
            '<th class="time-col">Time</th><th class="source-col">Source</th><th class="num">Buy&cent;</th><th class="num">Fees&cent;</th><th class="num">Sell&cent;</th><th class="num">P2P&cent;</th><th class="num">Load</th><th class="num">Solar</th><th class="num">Batt</th><th class="num">Grid</th><th class="num">SoC%</th><th class="num">Net$</th>' +
          '</tr></thead>' +
          '<tbody>' + forecastRows + '</tbody>' +
        '</table></div>' +
      '</div>' + // .table-col
      '</div>' + // .chart-table-grid
      '</div>' + // .col-right
      '</div>' + // .layout-grid
        '<div class="footer">' +
          '<span class="chip"><span class="dot" style="background:' + statusColor + '"></span>Solver: ' + solverStatus + (clamped !== undefined ? ' (' + clamped + ' clamped)' : '') + '</span>' +
          '<span class="chip">Solved in ' + (solveSecs !== undefined ? solveSecs + 's' : '?') + ' at ' + lastSolvedStr + '</span>' +
          healthChips.map((h) => '<span class="chip"><span class="dot" style="background:' + okColor(h.ent) + '"></span>' + h.label + '</span>').join('') +
          // Direct household ask (2026-09-07, Mark Purcell via phone):
          // "a button on the control card which calls action solve
          // now... rather than waiting for it to do it all on next
          // cycle." nimbus_load.solve_now already exists as a real,
          // no-argument service (services.yaml) that "reuses the exact
          // same solve path the periodic timer and price-triggered
          // solve both call" (README's own docs) -- this button is
          // pure UI wiring onto an already-real mechanism, not a new
          // one. this._solving guards against a double-click firing a
          // second overlapping call while one is still in flight.
          '<button class="solve-now-btn" id="solve-now-btn" ' + (this._solving ? 'disabled' : '') + '>' +
            (this._solving ? 'Solving…' : 'Solve Now') +
          '</button>' +
        '</div>' +
        // Real household finding: this used to hardcode a devhub-only
        // dashboard path ("/nimbus-devhub/solver") -- the exact same
        // per-household hardcoding class as nimbus issue #364, just a
        // raw href instead of a config field. Since that path doesn't
        // exist on any OTHER install's own dashboard, HA silently fell
        // through to whichever view happened to load by default (HAEO's,
        // on the real household's own system) instead of erroring
        // visibly. Fixed to HA's own universal integration-settings URL
        // (/config/integrations/integration/<domain>) -- this shows
        // every Nimbus hub instance with its real "Configure" action
        // right there, identically on every install, no per-household
        // config needed at all since the domain itself never changes.
        '<div class="tuning-row">' + tuningItems +
          '<a class="tuning-link" href="/config/integrations/integration/nimbus_load">Full Solver settings &rarr;</a>' +
        '</div>' +
        '<div class="section-label" style="margin:18px 0 8px;">Economics &amp; Quality</div>' +
        '<div class="tuning-row" style="border-top:none; padding-top:0; margin-top:0;">' + econItems + '</div>' +
      '</div>';

    // Restore the forecast table's own scroll position immediately -- see
    // the comment at the top of _render() for why this is needed at all.
    const newWrap = this.shadowRoot.querySelector('.ftable-wrap');
    if (newWrap && prevScrollTop) newWrap.scrollTop = prevScrollTop;
    if (newWrap && prevScrollLeft) newWrap.scrollLeft = prevScrollLeft;

    this.shadowRoot.querySelectorAll('.mode-chip').forEach(btn => {
      btn.addEventListener('click', () => this._setMode(btn.dataset.mode));
    });
    const killSwitch = this.shadowRoot.getElementById('kill-switch');
    if (killSwitch) killSwitch.addEventListener('click', () => this._toggleArmed());
    const solveNowBtn = this.shadowRoot.getElementById('solve-now-btn');
    if (solveNowBtn) solveNowBtn.addEventListener('click', () => this._solveNow());
    this.shadowRoot.querySelectorAll('.risk-slider').forEach(el => {
      const valueLabel = this.shadowRoot.querySelector('[data-risk-value="' + el.dataset.entity + '"]');
      const startDrag = () => { this._sliderDragging = true; };
      const endDrag = () => {
        this._sliderDragging = false;
        this._setRisk(el.dataset.entity, parseFloat(el.value));
      };
      el.addEventListener('pointerdown', startDrag);
      el.addEventListener('input', () => {
        if (valueLabel) valueLabel.textContent = parseFloat(el.value).toFixed(2);
        el.style.setProperty('--risk-pct', (parseFloat(el.value) * 100) + '%');
      });
      el.addEventListener('change', endDrag);
      // Safety net: if pointerup/change never fires for some reason (e.g. a
      // keyboard-driven change), don't leave the card permanently frozen.
      el.addEventListener('pointerup', () => { setTimeout(() => { this._sliderDragging = false; }, 0); });
    });
  }
}
customElements.define('nimbus-dispatch-card-v4', NimbusDispatchCardV4);
window.customCards = window.customCards || [];
window.customCards.push({ type: 'nimbus-dispatch-card-v4', name: 'Nimbus Dispatch Card v4', description: 'Unified Nimbus mode control, live decision gauge, dispatch timeline, and tuning stats' });
