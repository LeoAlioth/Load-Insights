/*
 * Load Insights card (the owner, 2026-10-31): the loads waiting for a name, or what is running now, with a name
 * field on every unnamed row that calls load_insights.name_load. Registered by the integration itself
 * (__init__._register_card), so no dashboard resource is needed.
 *
 *   type: custom:load-insights-card
 *   mode: to_name          # or: running
 *   title: To name         # optional
 *   max_rows: 10           # optional
 *   min_watts / max_watts            # size filter, both modes
 *   min_seconds / max_seconds        # to_name: a run's usual length; running: how long it has been on
 *   min_per_day / max_per_day        # to_name: runs a day (from its usual interval)
 *   min_certainty: 10                # to_name: percent
 *   seen_within_hours: 24            # to_name: last seen at most this long ago
 *   show: all                        # running: all, unnamed or named
 *                                    # to_name: the unnamed loads running now on top (any meter's), then the
 *                                    # rest seen within seen_within_hours under their own heading
 *   sort_by: certainty               # certainty (to_name), size, length, runs_per_day (to_name), recent
 *   library_entity: sensor.x_signature_library   # optional, found by its attributes otherwise
 *   detected_entity: sensor.x_detected_loads     # optional, found by its attributes otherwise
 *
 * Every option can be set in the card's visual editor too.
 *
 * A row's name field offers the names already in use: picking one joins the load to it.
 */
const DAY_S = 86400;
const INTEGRATION_URL = "/config/integrations/integration/load_insights";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const watts = (w) => (w >= 1000 ? `${(w / 1000).toFixed(1)} kW` : `${Math.round(w)} W`);
const dur = (s) => (s < 90 ? `~${Math.round(s)} s` : s < 5400 ? `~${Math.round(s / 60)} min` : `~${(s / 3600).toFixed(1)} h`);
const ago = (s) => (s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`);
const phases = (p) => String(p || "").split("").join("+");

const num = (v) => (v === undefined || v === null || v === "" ? null : Number(v));
const within = (v, lo, hi) => (lo === null || v >= lo) && (hi === null || v <= hi);

class LoadInsightsCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("load-insights-card-editor");
  }

  setConfig(config) {
    const mode = config.mode || "to_name";
    if (!["to_name", "running"].includes(mode)) throw new Error("mode must be to_name or running");
    this._config = { max_rows: 10, min_certainty: 10, seen_within_hours: 24, show: "all", sort_by: "certainty",
                     ...config, mode };
    this._typed = {};      // row key -> what was typed, kept across renders
    this._named = {};      // row key -> the name given, hidden until the library catches up
    this._errors = {};
    this._key = null;
  }

  static getStubConfig() {
    return { mode: "to_name" };
  }

  getCardSize() {
    return 2 + Math.min(this._config?.max_rows || 10, 10);
  }

  _entity(kind) {
    const hass = this._hass;
    const set = this._config[kind === "library" ? "library_entity" : "detected_entity"];
    if (set) return hass.states[set];
    // by what it carries; while Load Insights starts it carries nothing, so then by its entity id's ending
    const states = Object.values(hass.states).filter((s) => s.entity_id.startsWith("sensor."));
    return states.find((s) => kind === "library"
        ? Array.isArray(s.attributes.signatures) && s.attributes.meters !== undefined
        : Array.isArray(s.attributes.running_ids))
      || states.find((s) => s.entity_id.endsWith(kind === "library" ? "_signature_library" : "_detected_loads"));
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    const lib = this._entity("library");
    const det = this._entity("detected");
    const rows = this._rows(lib, det);
    // render only when what is shown changes, and never under a field being typed in
    const key = JSON.stringify(rows) + JSON.stringify(this._named) + JSON.stringify(this._errors) +
      `${lib?.entity_id}:${lib?.state === "unavailable"}:${det?.entity_id}:${det?.state === "unavailable"}`;
    if (key === this._key) return;
    if (this.contains(document.activeElement) && document.activeElement.tagName === "INPUT") {
      this._pending = true;
      return;
    }
    this._key = key;
    this._render(rows, lib, det);
  }

  _names(lib) {
    const names = new Set();
    for (const s of lib?.attributes.signatures || []) if (s.name) names.add(s.name);
    for (const m of Object.values(lib?.attributes.meters || {})) for (const s of m.signatures || []) if (s.name) names.add(s.name);
    return [...names].sort((a, b) => a.localeCompare(b));
  }

  _rows(lib, det) {
    const now = Date.now() / 1000;
    const max = this._config.max_rows;
    if (this._config.mode === "running") {
      const c = this._config;
      return (det?.attributes.running || [])
        .filter((r) => within(r.watts, num(c.min_watts), num(c.max_watts)) &&
          within(now - Date.parse(r.since) / 1000, num(c.min_seconds), num(c.max_seconds)) &&
          (c.show === "unnamed" ? !r.name : c.show === "named" ? !!r.name : true))
        .sort((a, b) => {
          const on = (r) => now - Date.parse(r.since) / 1000;
          if (c.sort_by === "length") return on(b) - on(a);
          if (c.sort_by === "recent") return on(a) - on(b);
          if (c.sort_by === "size") return b.watts - a.watts;
          return (b.name ? 1 : 0) - (a.name ? 1 : 0) || b.watts - a.watts;      // certainty means nothing here: named first
        })
        .slice(0, max)
        .map((r) => ({
          key: `${r.meter ?? ""}#${r.id}`, meter: r.meter ?? "", id: r.id, name: r.name,
          line: `${watts(r.watts)} on ${phases(r.phases)}, for ${dur(now - Date.parse(r.since) / 1000)}${r.where ? `, in ${r.where}` : ""}`,
          tag: r.tag || "",
        }))
        .filter((r) => r.name || (r.id !== null && r.id !== undefined && !this._named[r.key]));
    }
    const run = new Set(det?.attributes.running_ids || []);
    const c = this._config;
    const hours = num(c.seen_within_hours) ?? 24;
    const seen = hours * 3600;
    const sure = (num(c.min_certainty) ?? 10) / 100;
    // on top: what is running now unnamed, the grid's and every meter's (Fleet.running's rows)
    const on = (r) => now - Date.parse(r.since) / 1000;
    const running = (det?.attributes.running || [])
      .filter((r) => !r.name && r.id !== null && r.id !== undefined && !this._named[`${r.meter ?? ""}#${r.id}`] &&
        within(r.watts, num(c.min_watts), num(c.max_watts)))
      .sort((a, b) => c.sort_by === "recent" ? on(a) - on(b) : c.sort_by === "length" ? on(b) - on(a) : b.watts - a.watts)
      .slice(0, max)
      .map((r) => ({
        key: `${r.meter ?? ""}#${r.id}`, meter: r.meter ?? "", id: r.id, name: null,
        line: `${watts(r.watts)} on ${phases(r.phases)}, on for ${dur(on(r))}${r.where ? `, in ${r.where}` : ""}`,
        tag: r.tag || "",
      }));
    const rest = this._toName(lib, run, c, now, seen, sure, max);
    if (!running.length) return rest;
    return [{ header: "Running now" }, ...running, ...(rest.length ? [{ header: `Seen in the last ${hours} h` }, ...rest] : [])];
  }

  _toName(lib, run, c, now, seen, sure, max) {
    return (lib?.attributes.signatures || [])
      .filter((s) => !s.name && s.last_seen_ts >= now - seen && (s.evidence || 0) >= sure && !this._named[`#${s.id}`])
      .filter((s) => !run.has(s.id))                    // running now: in the section above
      .filter((s) => {
        const w = Object.values(s.watts_by_phase || {}).reduce((a, b) => a + b, 0);
        const perDay = s.typical_interval_s ? DAY_S / s.typical_interval_s : null;
        return within(w, num(c.min_watts), num(c.max_watts)) &&
          within(s.typical_duration_s || 0, num(c.min_seconds), num(c.max_seconds)) &&
          (num(c.min_per_day) === null && num(c.max_per_day) === null ||
           perDay !== null && within(perDay, num(c.min_per_day), num(c.max_per_day)));
      })
      .sort((a, b) => {
        const size = (x) => Object.values(x.watts_by_phase || {}).reduce((p, q) => p + q, 0);
        const perDay = (x) => (x.typical_interval_s ? DAY_S / x.typical_interval_s : 0);
        if (c.sort_by === "size") return size(b) - size(a);
        if (c.sort_by === "length") return (b.typical_duration_s || 0) - (a.typical_duration_s || 0);
        if (c.sort_by === "runs_per_day") return perDay(b) - perDay(a);
        if (c.sort_by === "recent") return b.last_seen_ts - a.last_seen_ts;
        return b.evidence - a.evidence;
      })
      .slice(0, max)
      .map((s) => {
        const w = Object.values(s.watts_by_phase || {}).reduce((a, b) => a + b, 0);
        const perDay = s.typical_interval_s ? DAY_S / s.typical_interval_s : null;      // what the filter reads
        const where = (s.where || "").startsWith("in ") ? s.where.split(",")[0] : "no meter";
        const guess = s.guess && s.guess.kind && s.guess.confidence >= 0.5 ? s.guess.kind.replace(/^(a|an) /, "") : "";
        const last = run.has(s.id) ? "running now" : ago(now - s.last_seen_ts);
        return {
          key: `#${s.id}`, meter: "", id: s.id, name: null,
          line: `${watts(w)} on ${phases(s.phases)}, runs ${dur(s.typical_duration_s || 0)}` +
                `${perDay ? `, ${perDay >= 1 ? Math.round(perDay) : perDay.toFixed(1)}× a day` : ""}, seen ${s.count}×, ${where}, ` +
                `${Math.round((s.evidence || 0) * 100)} % certain, ${last}`,
          tag: guess,
        };
      });
  }

  _render(rows, lib, det) {
    const c = this._config;
    const title = c.title ?? (c.mode === "running" ? "Running now" : "To name");
    const names = this._names(lib);
    const source = c.mode === "running" ? det : lib;
    const body = !source
      ? `<div class="empty">Load Insights' ${c.mode === "running" ? "detected loads" : "signature library"} sensor was not found.</div>`
      : ["unavailable", "unknown"].includes(source.state)
        ? `<div class="empty">Load Insights is starting - its loads show once it has read its history.</div>`
        : rows.length === 0
        ? `<div class="empty">${c.mode === "running" ? "Nothing detected running." : "Nothing waiting for a name."}</div>`
        : rows.map((r) => this._row(r)).join("");
    this.innerHTML = `
      <ha-card header="${esc(title)}">
        <style>
          .li-rows { padding: 0 16px 8px; }
          .li-row { padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
          .li-section { padding: 12px 0 2px; font-weight: 500; color: var(--secondary-text-color);
            text-transform: uppercase; font-size: 0.8em; letter-spacing: 0.05em; }
          .li-section:first-child { padding-top: 0; }
          .li-row:last-child { border-bottom: none; }
          .li-top { display: flex; align-items: baseline; gap: 8px; }
          .li-name { font-weight: 500; }
          .li-id { color: var(--secondary-text-color); font-size: 0.85em; text-decoration: none; }
          .li-tag { color: var(--secondary-text-color); font-size: 0.85em; font-style: italic; }
          .li-line { color: var(--secondary-text-color); font-size: 0.9em; margin: 2px 0 6px; }
          .li-form { display: flex; gap: 8px; }
          .li-form input { flex: 1; min-width: 0; padding: 6px 8px; border-radius: 6px;
            border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
          .li-form button { padding: 6px 12px; border-radius: 6px; border: none; cursor: pointer;
            background: var(--primary-color); color: var(--text-primary-color, #fff); }
          .li-form button:disabled { opacity: 0.5; cursor: default; }
          .li-err { color: var(--error-color); font-size: 0.85em; margin-top: 4px; }
          .empty { padding: 0 16px 16px; color: var(--secondary-text-color); }
          .li-foot { padding: 0 16px 12px; font-size: 0.85em; }
          .li-foot a { color: var(--primary-color); }
        </style>
        <datalist id="li-names-${c.mode}">${names.map((n) => `<option value="${esc(n)}"></option>`).join("")}</datalist>
        <div class="li-rows">${body}</div>
        <div class="li-foot"><a href="${INTEGRATION_URL}">Load Insights</a></div>
      </ha-card>`;
    this.querySelectorAll("form.li-form").forEach((f) => {
      const input = f.querySelector("input");
      input.addEventListener("input", () => { this._typed[f.dataset.key] = input.value; });
      input.addEventListener("blur", () => { if (this._pending) { this._pending = false; this._key = null; this.hass = this._hass; } });
      f.addEventListener("submit", (ev) => { ev.preventDefault(); this._name(f.dataset.key, f.dataset.meter, Number(f.dataset.id), input.value.trim(), f); });
    });
  }

  _row(r) {
    if (r.header) return `<div class="li-section">${esc(r.header)}</div>`;
    const head = r.name
      ? `<span class="li-name">${esc(r.name)}</span>`
      : `<span class="li-name">Unnamed</span> <a class="li-id" href="${INTEGRATION_URL}">#${esc(r.id)}</a>`;
    const form = r.name ? "" : `
      <form class="li-form" data-key="${esc(r.key)}" data-meter="${esc(r.meter)}" data-id="${esc(r.id)}">
        <input type="text" list="li-names-${this._config.mode}" placeholder="Name it, or pick a name in use to join it"
               value="${esc(this._typed[r.key] || "")}">
        <button type="submit">Name</button>
      </form>
      ${this._errors[r.key] ? `<div class="li-err">${esc(this._errors[r.key])}</div>` : ""}`;
    return `<div class="li-row">
      <div class="li-top">${head}${r.tag ? `<span class="li-tag">${esc(r.tag)}</span>` : ""}</div>
      <div class="li-line">${esc(r.line)}</div>${form}</div>`;
  }

  async _name(key, meter, id, name, form) {
    if (!name) return;
    const button = form.querySelector("button");
    button.disabled = true;
    delete this._errors[key];
    const data = { signature_id: id, name };
    if (meter) data.meter = meter;
    try {
      await this._hass.callService("load_insights", "name_load", data);
      this._named[key] = name;
      delete this._typed[key];
    } catch (err) {
      this._errors[key] = err?.message || String(err);
    }
    this._key = null;
    if (document.activeElement && this.contains(document.activeElement)) document.activeElement.blur();
    this.hass = this._hass;
  }
}

// The card's visual editor: Home Assistant's own form, every option above.
const EDITOR_SCHEMA = [
  { name: "mode", selector: { select: { mode: "dropdown", options: [
    { value: "to_name", label: "Loads to name" }, { value: "running", label: "Running now" }] } } },
  { name: "title", selector: { text: {} } },
  { name: "max_rows", selector: { number: { min: 1, max: 50, mode: "box" } } },
  { name: "sort_by", selector: { select: { mode: "dropdown", options: [
    { value: "certainty", label: "Certainty (to name) / named first (running)" }, { value: "size", label: "Size" },
    { value: "length", label: "Run length (to name) / on longest (running)" },
    { value: "runs_per_day", label: "Runs a day (to name)" }, { value: "recent", label: "Most recent" }] } } },
  { type: "grid", name: "", schema: [
    { name: "min_watts", selector: { number: { min: 0, mode: "box", unit_of_measurement: "W" } } },
    { name: "max_watts", selector: { number: { min: 0, mode: "box", unit_of_measurement: "W" } } },
    { name: "min_seconds", selector: { number: { min: 0, mode: "box", unit_of_measurement: "s" } } },
    { name: "max_seconds", selector: { number: { min: 0, mode: "box", unit_of_measurement: "s" } } },
    { name: "min_per_day", selector: { number: { min: 0, step: 0.1, mode: "box" } } },
    { name: "max_per_day", selector: { number: { min: 0, step: 0.1, mode: "box" } } },
    { name: "min_certainty", selector: { number: { min: 0, max: 100, mode: "box", unit_of_measurement: "%" } } },
    { name: "seen_within_hours", selector: { number: { min: 1, max: 240, mode: "box", unit_of_measurement: "h" } } },
  ] },
  { name: "show", selector: { select: { mode: "dropdown", options: [
    { value: "all", label: "All" }, { value: "unnamed", label: "Unnamed only" }, { value: "named", label: "Named only" }] } } },
];
const EDITOR_LABELS = {
  mode: "Show", title: "Title", max_rows: "Most rows", sort_by: "Sort by",

  min_watts: "Smallest", max_watts: "Largest",
  min_seconds: "Shortest run (to name) / on at least (running)", max_seconds: "Longest run (to name) / on at most (running)",
  min_per_day: "Fewest runs a day (to name)", max_per_day: "Most runs a day (to name)",
  min_certainty: "Least certainty (to name)", seen_within_hours: "Seen within (to name)",
  show: "Which (running now)",
};

class LoadInsightsCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
  }

  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => EDITOR_LABELS[s.name] || s.name;
      this._form.addEventListener("value-changed", (ev) => {
        const config = { ...ev.detail.value };
        for (const k of Object.keys(config)) if (config[k] === "" || config[k] === null || config[k] === undefined) delete config[k];
        this._config = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    this._form.data = this._config;
  }
}

if (!customElements.get("load-insights-card-editor")) customElements.define("load-insights-card-editor", LoadInsightsCardEditor);

if (!customElements.get("load-insights-card")) {
  customElements.define("load-insights-card", LoadInsightsCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "load-insights-card",
    name: "Load Insights",
    description: "Loads waiting for a name, or running now, named in place.",
  });
}
