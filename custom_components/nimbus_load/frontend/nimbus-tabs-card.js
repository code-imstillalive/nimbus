// nimbus-tabs-card.js
//
// Household, 3 Nov 2026: a release that changes a Nimbus tab (a new setting
// on the Solver tab) must reach every install without hand-typed YAML, and,
// the household's rule, the revised tab goes BESIDE the old one, never in
// place of it, so the household decides.
//
// Nimbus itself writes no dashboard (HACS review, hacs/default#12099:
// "An integration should not write the user's Home Assistant configuration
// on its own"; tests/test_hacs_no_dashboard_writes.py). This card does the
// update in the browser, only when the person viewing it presses the
// button, through the same public `lovelace/config/save` the dashboard
// editor uses, with that person's own login (an admin is needed, as for any
// dashboard edit). So it is the household editing their own dashboard,
// with Nimbus supplying the tabs (`nimbus_load.get_dashboard`, read-only).
//
// What it does, on the dashboard it is placed on:
//   1. "Check for updates": reads this dashboard and the standard tabs from
//      the installed Nimbus, and lists each standard tab as up to date,
//      changed, or missing (matched by the tab's path: solver, help, ...).
//   2. "Add updated tabs beside mine": for each changed tab, puts the new
//      version right after the existing one, as "<Title> (new)" at
//      "<path>-new"; a missing tab is added under its own name. An earlier
//      "(new)" copy is replaced, since it was proposed by this card. The
//      household's own tabs are never changed or removed.
//
// Storage-mode dashboards only (a YAML dashboard can't be saved this way;
// the card says so).

const NIMBUS_TABS_NEW = "-new";

function nimbusTabsEscape(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function nimbusTabsCanonical(value) {
  // JSON with keys sorted, so a tab equal in content compares equal.
  if (Array.isArray(value)) return "[" + value.map(nimbusTabsCanonical).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort()
      .map((k) => JSON.stringify(k) + ":" + nimbusTabsCanonical(value[k]))
      .join(",") + "}";
  }
  return JSON.stringify(value);
}

class NimbusTabsCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    this._plan = null;
    this._message = "";
    this._busy = false;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._render();
  }

  getCardSize() {
    return 3;
  }

  getGridOptions() {
    return { columns: "full", rows: "auto" };
  }

  _urlPath() {
    // The dashboard this card is on: the first path segment. "lovelace" is
    // the default dashboard, which the websocket API names null.
    const seg = (window.location.pathname.split("/")[1] || "").trim();
    return !seg || seg === "lovelace" ? null : seg;
  }

  async _load() {
    const urlPath = this._urlPath();
    const config = await this._hass.callWS({ type: "lovelace/config", url_path: urlPath });
    const res = await this._hass.callWS({
      type: "call_service",
      domain: "nimbus_load",
      service: "get_dashboard",
      service_data: {},
      return_response: true,
    });
    const standard = ((res && res.response) || {}).views || [];
    return { urlPath, config, standard };
  }

  async _check() {
    this._busy = true;
    this._message = "Checking...";
    this._render();
    try {
      const { config, standard } = await this._load();
      const views = (config && config.views) || [];
      const items = standard.map((tab) => {
        const mine = views.find((v) => v.path === tab.path);
        const offered = views.find((v) => v.path === tab.path + NIMBUS_TABS_NEW);
        const want = nimbusTabsCanonical(tab);
        let status;
        if (!mine) status = "missing";
        else if (nimbusTabsCanonical(mine) === want) status = "same";
        else if (offered && nimbusTabsCanonical({ ...offered, path: tab.path, title: tab.title }) === want) status = "offered";
        else status = "changed";
        return { title: tab.title, path: tab.path, status };
      });
      this._plan = items;
      const todo = items.filter((i) => i.status === "changed" || i.status === "missing").length;
      this._message = todo
        ? `${todo} tab${todo === 1 ? "" : "s"} can be added beside yours.`
        : "Your Nimbus tabs are up to date.";
    } catch (e) {
      this._plan = null;
      this._message = "Could not check: " + ((e && e.message) || e);
    }
    this._busy = false;
    this._render();
  }

  async _apply() {
    this._busy = true;
    this._message = "Adding...";
    this._render();
    try {
      // Read again so nothing edited since the check is lost.
      const { urlPath, config, standard } = await this._load();
      const views = (config && config.views) ? [...config.views] : [];
      const added = [];
      for (const tab of standard) {
        const mineIdx = views.findIndex((v) => v.path === tab.path);
        if (mineIdx >= 0 && nimbusTabsCanonical(views[mineIdx]) === nimbusTabsCanonical(tab)) continue;
        if (mineIdx < 0) {
          views.push(JSON.parse(JSON.stringify(tab)));
          added.push(tab.title);
          continue;
        }
        const copy = JSON.parse(JSON.stringify(tab));
        copy.path = tab.path + NIMBUS_TABS_NEW;
        copy.title = tab.title + " (new)";
        const offeredIdx = views.findIndex((v) => v.path === copy.path);
        if (offeredIdx >= 0) views[offeredIdx] = copy;
        else views.splice(mineIdx + 1, 0, copy);
        added.push(copy.title);
      }
      if (!added.length) {
        this._message = "Nothing to add: your Nimbus tabs are up to date.";
      } else {
        await this._hass.callWS({
          type: "lovelace/config/save",
          url_path: urlPath,
          config: { ...config, views },
        });
        this._message = `Added beside your tabs: ${added.join(", ")}. Reload the page to see them; delete your old tab when you're happy with the new one.`;
      }
      this._plan = null;
    } catch (e) {
      this._message = "Could not add the tabs: " + ((e && e.message) || e)
        + " (a YAML dashboard can't be changed from here).";
    }
    this._busy = false;
    this._render();
  }

  _render() {
    if (!this.shadowRoot) return;
    const label = { same: "up to date", changed: "changed", missing: "not on this dashboard", offered: "new version already added" };
    const rows = (this._plan || [])
      .map((i) => `<li><b>${nimbusTabsEscape(i.title)}</b>: ${label[i.status]}</li>`)
      .join("");
    const todo = (this._plan || []).some((i) => i.status === "changed" || i.status === "missing");
    this.shadowRoot.innerHTML = `
      <style>
        .card-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        button { background: var(--primary-color); color: var(--text-primary-color, #fff);
                 border: none; border-radius: 4px; padding: 8px 14px; cursor: pointer; font: inherit; }
        button[disabled] { opacity: 0.5; cursor: default; }
      </style>
      <ha-card header="Nimbus tabs">
        <div class="card-content">
          <p>Compare this dashboard's tabs with the installed Nimbus release. Updated tabs are added beside yours; yours are never changed.</p>
          ${rows ? `<ul>${rows}</ul>` : ""}
          ${this._message ? `<p><i>${nimbusTabsEscape(this._message)}</i></p>` : ""}
        </div>
        <div class="card-actions">
          <button id="check" ${this._busy ? "disabled" : ""}>Check for updates</button>
          ${todo ? `<button id="apply" ${this._busy ? "disabled" : ""}>Add updated tabs beside mine</button>` : ""}
        </div>
      </ha-card>`;
    const check = this.shadowRoot.getElementById("check");
    if (check) check.addEventListener("click", () => this._check());
    const apply = this.shadowRoot.getElementById("apply");
    if (apply) apply.addEventListener("click", () => this._apply());
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

nimbusRegisterCard("nimbus-tabs-card", NimbusTabsCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "nimbus-tabs-card",
  name: "Nimbus Tabs",
  description:
    "Checks this dashboard's Nimbus tabs against the installed release and, on your click, adds updated tabs beside yours.",
});
