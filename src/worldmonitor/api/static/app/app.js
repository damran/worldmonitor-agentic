/* WorldMonitor consumption dashboard (ADR 0115, Slice D).
 * Vanilla JS over the public /api/dashboard read API. globe.gl (3D globe) + force-graph (2D
 * relationship panel), both vendored/self-hosted. No framework, no build step. */
"use strict";

const API = "/api/dashboard";
const $ = (id) => document.getElementById(id);

async function api(path) {
  const res = await fetch(API + path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

/* Fixed categorical palette for entity schemas (dataviz: fixed order, never cycled/hashed;
 * validated for the dark panel surface). Identity is never color-alone — every use sits next to
 * a text label (panel rows, graph legend, hover labels). */
const SCHEMA_COLORS = {
  Person: "#3987e5",
  Company: "#199e70",
  Organization: "#c98500",
  Event: "#9085e9",
  Address: "#e66767",
  Article: "#d95926",
};
// hasOwnProperty guard: a hostile schema string like "constructor" must hit the fallback, never
// an inherited Object.prototype member (adversarial-review catch).
const schemaColor = (s) =>
  Object.prototype.hasOwnProperty.call(SCHEMA_COLORS, s) ? SCHEMA_COLORS[s] : "#898781";

const relTime = (iso) => {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (isNaN(t)) return String(iso).slice(0, 10);
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return mins + "m ago";
  const hours = Math.round(mins / 60);
  if (hours < 48) return hours + "h ago";
  return Math.round(hours / 24) + "d ago";
};

// Last-fetched data, cached so the side panels re-render from either loader.
let lastPoints = [];
let lastArticles = [];

let world, fg;

function initGlobe() {
  const el = $("globe");
  world = Globe()(el)
    .backgroundColor("rgba(0,0,0,0)")
    .showGlobe(true)
    .showGraticules(true)
    .showAtmosphere(true)
    .atmosphereColor("#4fd1c5")
    .atmosphereAltitude(0.16)
    .pointLat("lat")
    .pointLng("lon")
    .pointColor((d) => (d.geo_precision === "point" ? "#4fd1c5" : "#f6ad55"))
    .pointAltitude((d) => (d.geo_precision === "point" ? 0.07 : 0.02))
    .pointRadius((d) => (d.geo_precision === "point" ? 0.4 : 0.55))
    .pointLabel(
      (d) =>
        `<div style="font:12px sans-serif;color:#dfe7f2">${esc(d.label || d.id)}` +
        `${d.country ? " · " + esc(d.country.toUpperCase()) : ""}</div>`,
    )
    .onPointClick((d) => loadEntity(d.id));

  // Texture-less dark globe (no external image => no CDN); graticules give it structure.
  const mat = world.globeMaterial();
  mat.color.set("#0d1a2b");
  mat.emissive.set("#0a1420");
  mat.shininess = 6;

  world.controls().autoRotate = true;
  world.controls().autoRotateSpeed = 0.3;
  world.pointOfView({ lat: 20, lng: 10, altitude: 2.4 });
  // Clicking a small dot on a MOVING globe is nearly impossible — stop the idle rotation the
  // moment the user first interacts (they can orbit manually from then on).
  el.addEventListener("pointerdown", () => {
    world.controls().autoRotate = false;
  }, { once: true });

  // Country outlines (vendored Natural Earth 110m, public domain — no CDN): the "world map"
  // layer so dots have continents to sit on. Sits below the point altitudes so dots stay
  // clickable above it.
  fetch("vendor/countries.geojson")
    .then((r) => (r.ok ? r.json() : null))
    .then((geo) => {
      if (!geo || !geo.features) return;
      world
        .polygonsTransitionDuration(0)
        .polygonsData(geo.features)
        .polygonCapColor(() => "rgba(37,66,98,0.55)")
        .polygonSideColor(() => "rgba(0,0,0,0)")
        .polygonStrokeColor(() => "#3b5f88")
        .polygonAltitude(0.004);
    })
    .catch((e) => console.warn("country outlines unavailable", e));

  // Debug/testability handle (read-only usage): lets a headless harness (and an operator
  // console) inspect pointsData()/polygonsData() and drive handlers directly.
  window.__wmGlobe = world;

  const size = () => world.width(el.clientWidth).height(el.clientHeight);
  size();
  window.addEventListener("resize", size);
}

/* Deterministic display-only scatter for country-precision dots: entities sharing one country
 * centroid would render as a single unclickable dot, so each is offset ~±1.5° around it. The dot
 * still overlaps neighbours in dense countries, but every dot gets its own raycast target.
 * geo_precision "country" already declares the location approximate; the true country stays on
 * the point. Seeded FNV-1a (xor+imul) => the lat/lon axes are INDEPENDENT — a suffix on a
 * polynomial hash is NOT (both offsets would differ by a constant, collapsing the scatter onto
 * one diagonal line and re-stacking dots; found by adversarial review, verified numerically). */
function jitterCountryPoints(points) {
  const hash01 = (s, seed) => {
    let h = seed >>> 0;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    return (h % 100000) / 100000;
  };
  points.forEach((p) => {
    if (p.geo_precision !== "country") return;
    p.lat += (hash01(p.id, 0x811c9dc5) - 0.5) * 3;
    p.lon += (hash01(p.id, 0x01000193) - 0.5) * 3;
    if (p.lat > 89) p.lat = 89;
    if (p.lat < -89) p.lat = -89;
  });
  return points;
}

function setStatus(ok) {
  const el = $("status");
  if (!el) return;
  el.classList.toggle("ok", ok);
  el.classList.toggle("err", !ok);
  $("status-text").textContent = ok ? "LIVE" : "OFFLINE";
}

async function loadStats() {
  try {
    const s = await api("/stats");
    $("stat-nodes").textContent = s.nodes.toLocaleString();
    $("stat-edges").textContent = s.edges.toLocaleString();
    $("stat-articles").textContent = s.articles.toLocaleString();
    setStatus(true);
    const fresh = $("freshness");
    if (fresh) {
      fresh.textContent = "updated " + new Date().toISOString().slice(11, 19) + " UTC";
    }
  } catch (e) {
    setStatus(false);
    console.warn(e);
  }
}

async function loadPoints() {
  try {
    const { points } = await api("/points?limit=400");
    lastPoints = points;
    world.pointsData(jitterCountryPoints(points));
    renderSidePanels();
  } catch (e) {
    console.warn(e);
  }
}

/* ---- left intel panels: client-side aggregations over the already-fetched data ---- */
function barRows(counts, colorFor) {
  const entries = Object.keys(counts)
    .map((k) => [k, counts[k]])
    .sort((a, b) => b[1] - a[1]);
  if (!entries.length) return "";
  const max = entries[0][1];
  return entries
    .map(([label, n]) => {
      const dot = colorFor ? `<i style="background:${esc(colorFor(label))}"></i>` : "";
      const pct = Math.max(3, Math.round((n / max) * 100));
      return (
        `<div class="brow"><span class="lbl">${dot}${esc(label)}</span>` +
        `<span class="track"><span class="fill" style="width:${pct}%"></span></span>` +
        `<span class="val">${n.toLocaleString()}</span></div>`
      );
    })
    .join("");
}

function renderSidePanels() {
  const types = $("types-body");
  const countries = $("countries-body");
  const sources = $("sources-body");
  if (!types) return; // narrow layout: the side column is hidden

  // Object.create(null): the keys are HOSTILE strings (schema/country from the graph, publisher
  // from RSS) — a feed publisher literally named "constructor" would otherwise read an inherited
  // Object.prototype member and poison the counts (adversarial-review catch).
  const topN = (counts, n) => {
    const top = Object.create(null);
    Object.keys(counts)
      .sort((a, b) => counts[b] - counts[a])
      .slice(0, n)
      .forEach((k) => {
        top[k] = counts[k];
      });
    return top;
  };

  const byType = Object.create(null);
  const byCountry = Object.create(null);
  lastPoints.forEach((p) => {
    if (p.schema) byType[p.schema] = (byType[p.schema] || 0) + 1;
    if (p.country) {
      const c = String(p.country).toUpperCase();
      byCountry[c] = (byCountry[c] || 0) + 1;
    }
  });
  const topTypes = topN(byType, 6);
  const topCountries = topN(byCountry, 8);

  const byPublisher = Object.create(null);
  lastArticles.forEach((a) => {
    if (a.publisher) byPublisher[a.publisher] = (byPublisher[a.publisher] || 0) + 1;
  });
  const topPublishers = topN(byPublisher, 5);

  types.innerHTML = barRows(topTypes, schemaColor) || `<div class="hint-sm">No entities yet.</div>`;
  countries.innerHTML = barRows(topCountries) || `<div class="hint-sm">No geo data yet.</div>`;
  sources.innerHTML =
    barRows(topPublishers) || `<div class="hint-sm">No feed items yet.</div>`;
}

function renderTicker() {
  const inner = $("ticker-inner");
  if (!inner || !lastArticles.length) return;
  const items = lastArticles
    .slice(0, 25)
    .map(
      (a) =>
        `<span class="t-item"><b>${esc(a.publisher || "•")}</b>${esc(a.title || "")}</span>`,
    )
    .join("");
  inner.innerHTML = items + items; // duplicated for a seamless marquee loop
  // Constant READABLE speed (~60px/s) regardless of content volume: a fixed duration scales the
  // speed with content width (25 long headlines -> ~255px/s, unreadable).
  const distance = inner.scrollWidth / 2;
  if (distance > 0) inner.style.animationDuration = Math.max(30, Math.round(distance / 60)) + "s";
}

function articleCard(a) {
  const when = relTime(a.published || a.retrieved_at);
  return (
    `<div class="card" data-id="${esc(a.id)}">` +
    `<div class="title">${esc(a.title || a.id)}</div>` +
    `<div class="meta">${a.publisher ? `<span class="pub">${esc(a.publisher)}</span>` : ""}` +
    `<span>${esc(when)}</span></div></div>`
  );
}

async function loadFeed() {
  try {
    const { articles } = await api("/feed?limit=60");
    lastArticles = articles;
    const feed = $("feed");
    feed.innerHTML = articles.length
      ? articles.map(articleCard).join("")
      : `<div class="hint">No articles yet — the driver ingests curated feeds on its cadence.</div>`;
    feed.querySelectorAll(".card").forEach((c) =>
      c.addEventListener("click", () => loadEntity(c.dataset.id)),
    );
    renderTicker();
    renderSidePanels();
  } catch (e) {
    console.warn(e);
  }
}

async function loadBrief() {
  try {
    const data = await api("/brief");
    const body = $("brief-body");
    body.textContent = data.brief || "";
    const sources = (data.sources || []).filter((s) => s.url).slice(0, 6);
    if (sources.length) {
      const cites = sources
        .map((s, i) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">[${i + 1}]</a>`)
        .join(" ");
      body.insertAdjacentHTML("beforeend", ` <span class="cites">${cites}</span>`);
    }
  } catch (e) {
    console.warn(e);
  }
}

function renderGraph(data) {
  const el = $("graph-canvas");
  if (!fg) {
    fg = ForceGraph()(el)
      .backgroundColor("#0d131f")
      .nodeRelSize(4)
      .nodeVal((n) => (n.center ? 6 : 2))
      .nodeLabel((n) => `${esc(n.label || n.id)}${n.schema ? " · " + esc(n.schema) : ""}`)
      .nodeColor((n) => (n.center ? "#4fd1c5" : schemaColor(n.schema)))
      .linkColor(() => "#2a3a52")
      .linkLabel((l) => esc(l.rel || ""))
      .onNodeClick((n) => loadEntity(n.id));
  }
  fg.width(el.clientWidth).height(el.clientHeight).graphData(data);

  // Legend for the schemas actually present (identity never color-alone).
  const legend = $("graph-legend");
  if (legend) {
    const present = {};
    data.nodes.forEach((n) => {
      if (n.schema) present[n.schema] = true;
    });
    const entries = Object.keys(present).map(
      (s) => `<span><i style="background:${esc(schemaColor(s))}"></i>${esc(s)}</span>`,
    );
    entries.unshift(`<span><i style="background:#4fd1c5"></i>selected</span>`);
    legend.innerHTML = entries.join("");
  }
}

function renderReceipts(entity) {
  const p = entity.provenance || {};
  const props = entity.properties || {};
  const first = (v) => (Array.isArray(v) ? v[0] : v);
  const rows = [
    ["id", entity.id],
    ["name", first(props.name) || first(props.title)],
    ["country", first(props.country)],
    ["source", p.prov_source_id],
    ["retrieved", p.prov_retrieved_at],
    ["reliability", p.prov_reliability],
    ["raw record", p.prov_source_record],
  ].filter(([, v]) => v);
  $("receipts").innerHTML =
    `<h4>PROVENANCE · RECEIPTS</h4>` +
    rows
      .map(([k, v]) => `<div class="row"><span class="k">${esc(k)}</span><span>${esc(v)}</span></div>`)
      .join("");
}

async function loadEntity(id) {
  showTab("graph");
  $("graph-empty").style.display = "none";
  try {
    const entity = await api(`/entity/${encodeURIComponent(id)}`);
    renderGraph({ nodes: entity.nodes, links: entity.links });
    renderReceipts(entity);
  } catch (e) {
    console.warn(e);
    $("receipts").innerHTML = `<div class="hint">Could not load ${esc(id)} (${esc(e.message)}).</div>`;
  }
}

async function runSearch(term) {
  try {
    const { results } = await api(`/search?q=${encodeURIComponent(term)}&limit=25`);
    const feed = $("feed");
    showTab("feed");
    feed.innerHTML =
      `<div class="hint">Results for “${esc(term)}” (${results.length})</div>` +
      results
        .map(
          (r) =>
            `<div class="card" data-id="${esc(r.id)}"><div class="title">${esc(r.label || r.id)}</div>` +
            `<div class="meta"><span class="badge">${esc((r.labels || []).filter((l) => l !== "Entity")[0] || "Entity")}</span></div></div>`,
        )
        .join("");
    feed.querySelectorAll(".card").forEach((c) =>
      c.addEventListener("click", () => loadEntity(c.dataset.id)),
    );
  } catch (e) {
    console.warn(e);
  }
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === name));
  if (name === "graph" && fg) {
    const el = $("graph-canvas");
    fg.width(el.clientWidth).height(el.clientHeight);
  }
}

function startClock() {
  const el = $("clock");
  if (!el) return;
  const tickClock = () => {
    el.textContent = new Date().toISOString().slice(11, 19) + " UTC";
  };
  tickClock();
  setInterval(tickClock, 1000);
}

function init() {
  initGlobe();
  startClock();
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => showTab(t.dataset.tab)),
  );
  $("search").addEventListener("submit", (e) => {
    e.preventDefault();
    const term = $("q").value.trim();
    if (term) runSearch(term);
  });
  loadStats();
  loadPoints();
  loadFeed();
  loadBrief();
  // Refresh the live layers periodically (the driver ingests on its cadence).
  setInterval(loadStats, 60000);
  setInterval(loadPoints, 120000);
  setInterval(loadFeed, 120000);
  setInterval(loadBrief, 300000);
}

window.addEventListener("DOMContentLoaded", init);
