/*
 * Load Insights Forecast card (the owner, 2026-11-01; spec in Load Juggler's
 * docs/plans/2026-11-01/dashboard-cards.md): one chart, a dropdown to pick the forecast. Drawn by the card
 * itself as SVG - no plotly or config-template card needed. Registered by the integration itself
 * (__init__._register_card), so no dashboard resource is needed.
 *
 *   type: custom:load-insights-forecast-card
 *   title: Forecast        # optional
 *   include: [sensor.x]    # only these, in this order (the first is shown when the card opens)
 *   exclude: [sensor.y]    # or: every forecast except these - not both
 *   hours_back: 36         # optional
 *   hours_ahead: 36        # optional
 *
 * A forecast is any Load Insights sensor carrying a `detailedForecast` attribute (hourly kWh, with kwh_p10 /
 * kwh_p90) - the actual hours come from its `history` attribute.
 */
const HOUR = 3600e3;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const kw = (w) => (w >= 1000 ? `${(w / 1000).toFixed(1)}k` : `${Math.round(w)}`);

class LoadInsightsForecastCard extends HTMLElement {
  static getConfigForm() {
    const forecasts = { entity: { integration: "load_insights", domain: "sensor", multiple: true } };
    return {
      schema: [
        { name: "title", selector: { text: {} } },
        { name: "include", selector: forecasts },
        { name: "exclude", selector: forecasts },
        { name: "hours_back", selector: { number: { min: 6, max: 168, step: 6, mode: "box" } } },
        { name: "hours_ahead", selector: { number: { min: 6, max: 168, step: 6, mode: "box" } } },
      ],
    };
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    if (config.include?.length && config.exclude?.length) {
      throw new Error("Set include or exclude, not both");
    }
    this._config = { title: "Forecast", hours_back: 36, hours_ahead: 36, ...config };
    this._selected = null;
    this._key = null;
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    const list = this._forecasts();
    if (!list.some(([e]) => e === this._selected)) this._selected = list[0]?.[0] ?? null;
    const state = this._selected ? hass.states[this._selected] : null;
    // Redraw when the list, the pick or the forecast changed, and at least every 5 minutes for the "now" line.
    const key = JSON.stringify([list, this._selected, state?.last_updated, Math.floor(Date.now() / 300e3)]);
    if (key === this._key) return;
    this._key = key;
    this._list = list;
    this._render();
  }

  /** [[entity_id, name], ...]: the forecasts to offer, after include / exclude. */
  _forecasts() {
    const h = this._hass;
    const isForecast = (e) => e?.startsWith("sensor.") && h.states[e]?.attributes?.detailedForecast !== undefined;
    let ids;
    if (this._config.include?.length) {
      ids = this._config.include.filter(isForecast);
    } else {
      const skip = new Set(this._config.exclude || []);
      ids = Object.keys(h.states).filter((e) => isForecast(e) && !skip.has(e))
        .sort((a, b) => this._name(a).localeCompare(this._name(b)));
    }
    return ids.map((e) => [e, this._name(e)]);
  }

  /** The friendly name without its device's name in front and "forecast" at the end. */
  _name(entity) {
    const h = this._hass;
    let name = h.states[entity]?.attributes?.friendly_name || entity;
    const device = h.devices?.[h.entities?.[entity]?.device_id];
    const prefix = device?.name_by_user || device?.name;
    if (prefix && name.toLowerCase().startsWith(prefix.toLowerCase() + " ")) {
      const rest = name.slice(prefix.length + 1);
      if (!/^forecast$/i.test(rest)) name = rest;
    }
    name = name.replace(/\s+forecast$/i, "") || name;
    return name.charAt(0).toUpperCase() + name.slice(1);
  }

  _render() {
    if (!this._card) {
      this._card = document.createElement("ha-card");
      (this.shadowRoot || this.attachShadow({ mode: "open" })).appendChild(this._card);
      this._card.innerHTML = `<style>${STYLE}</style>
        <div class="head"><div class="title"></div><div class="pick"></div></div>
        <div class="legend">bars: actual · dotted: forecast · band: likely range (W)</div>
        <div class="chart"></div>`;
    }
    this._card.querySelector(".title").textContent = this._config.title;
    this._renderPicker();
    this._card.querySelector(".chart").innerHTML = this._selected
      ? this._chart(this._hass.states[this._selected]?.attributes || {})
      : `<div class="none">No forecasts found.</div>`;
  }

  /** HA's own dropdown (ha-select), so the card matches the dashboard; a plain select where it is missing. */
  _renderPicker() {
    const pick = this._card.querySelector(".pick");
    const options = this._list.map(([value, label]) => ({ value, label }));
    const sig = JSON.stringify(options);
    if (customElements.get("ha-select")) {
      let sel = pick.querySelector("ha-select");
      if (!sel) {
        sel = document.createElement("ha-select");
        sel.naturalMenuWidth = true;
        sel.fixedMenuPosition = true;
        // Newer HA hands the new value in detail.value and leaves setting it to the card; older HA
        // (mwc) has already set sel.value.
        sel.addEventListener("selected", (ev) => {
          const value = ev.detail?.value ?? sel.value;
          if (!value || value === this._selected) return;
          this._selected = value;
          sel.value = value;
          this._key = null;
          this.hass = this._hass;
        });
        sel.addEventListener("closed", (ev) => ev.stopPropagation());
        pick.appendChild(sel);
      }
      if (sel._sig !== sig) {
        sel._sig = sig;
        if ("options" in sel || customElements.get("ha-select").elementProperties?.has("options")) {
          sel.options = options;
        } else {
          sel.innerHTML = options.map((o) => `<mwc-list-item value="${esc(o.value)}">${esc(o.label)}</mwc-list-item>`).join("");
        }
      }
      if (sel.value !== this._selected) sel.value = this._selected;
      return;
    }
    if (pick.dataset.sig !== sig) {
      pick.dataset.sig = sig;
      pick.innerHTML = `<select>${options.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")}</select>`;
      pick.querySelector("select").onchange = (ev) => { this._selected = ev.target.value; this._key = null; this.hass = this._hass; };
    }
    pick.querySelector("select").value = this._selected;
  }

  _chart(attrs) {
    const now = Date.now();
    const t0 = now - this._config.hours_back * HOUR, t1 = now + this._config.hours_ahead * HOUR;
    const inside = (r) => r.t >= t0 && r.t <= t1;
    const ahead = (attrs.detailedForecast || []).map((p) => ({
      t: +new Date(p.period_start), v: 1000 * p.kwh, lo: 1000 * (p.kwh_p10 ?? p.kwh), hi: 1000 * (p.kwh_p90 ?? p.kwh),
    })).filter(inside);
    const past = (attrs.history || []).map((p) => ({ t: +new Date(p.period_start), v: 1000 * p.kwh })).filter(inside);
    const W = 520, H = 230, L = 44, R = 8, T = 10, B = 28;
    const top = Math.max(10, ...ahead.map((p) => p.hi), ...past.map((p) => p.v)) * 1.1;
    const X = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
    const Y = (v) => T + (1 - v / top) * (H - T - B);
    let svg = "";
    for (let k = 0; k <= 4; k++) {
      const v = (top * k) / 4;
      svg += `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" class="grid"/>
        <text x="${L - 4}" y="${Y(v) + 4}" text-anchor="end" class="axis">${kw(v)}</text>`;
    }
    for (let t = Math.ceil(t0 / HOUR) * HOUR; t <= t1; t += HOUR) {
      const d = new Date(t);
      if (d.getHours() % 6) continue;
      const label = d.getHours() ? `${d.getHours()}h` : d.toLocaleDateString(undefined, { weekday: "short" });
      svg += `<text x="${X(t)}" y="${H - 10}" text-anchor="middle" class="axis">${label}</text>`;
    }
    const bar = Math.max(2, ((W - L - R) / ((t1 - t0) / HOUR)) * 0.8);
    for (const p of past) {
      svg += `<rect x="${X(p.t)}" y="${Y(p.v)}" width="${bar}" height="${Y(0) - Y(p.v)}" class="actual">
        <title>${new Date(p.t).toLocaleString()}: ${Math.round(p.v)} W</title></rect>`;
    }
    if (ahead.length) {
      const pts = (key) => ahead.map((p) => `${X(p.t)},${Y(p[key])}`);
      svg += `<path d="M${pts("hi").join(" L")} L${pts("lo").reverse().join(" L")} Z" class="band"/>
        <path d="M${pts("v").join(" L")}" class="line"/>`;
    }
    svg += `<line x1="${X(now)}" x2="${X(now)}" y1="${T}" y2="${H - B}" class="now"/>
      <text x="${X(now) + 3}" y="${T + 10}" class="axis">now</text>`;
    return `<svg viewBox="0 0 ${W} ${H}">${svg}</svg>`;
  }
}

const STYLE = `
  .head { display:flex; justify-content:space-between; align-items:center; padding:8px 16px 0; gap:8px; }
  .title { font-size:var(--ha-card-header-font-size, 20px); }
  .pick ha-select { min-width:180px; }
  .legend { padding:0 16px; font-size:12px; color:var(--secondary-text-color); }
  .none { padding:16px; opacity:.6; }
  svg { width:100%; display:block; padding:0 8px 8px; box-sizing:border-box; }
  .grid { stroke:var(--divider-color); }
  .axis { font-size:10px; fill:var(--secondary-text-color); }
  .actual { fill:rgba(120,170,120,0.65); }
  .band { fill:rgba(130,130,190,0.18); }
  .line { stroke:var(--primary-color); stroke-width:2; stroke-dasharray:4 3; fill:none; }
  .now { stroke:var(--secondary-text-color); stroke-dasharray:2 2; }
`;

customElements.define("load-insights-forecast-card", LoadInsightsForecastCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "load-insights-forecast-card",
  name: "Load Insights Forecast",
  description: "One forecast at a time against what actually happened, picked from a dropdown.",
});
