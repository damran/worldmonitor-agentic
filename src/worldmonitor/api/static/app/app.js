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
    .pointRadius(0.26)
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

  const size = () => world.width(el.clientWidth).height(el.clientHeight);
  size();
  window.addEventListener("resize", size);
}

async function loadStats() {
  try {
    const s = await api("/stats");
    $("stat-nodes").textContent = s.nodes.toLocaleString();
    $("stat-edges").textContent = s.edges.toLocaleString();
    $("stat-articles").textContent = s.articles.toLocaleString();
  } catch (e) {
    console.warn(e);
  }
}

async function loadPoints() {
  try {
    const { points } = await api("/points?limit=400");
    world.pointsData(points);
  } catch (e) {
    console.warn(e);
  }
}

function articleCard(a) {
  const when = a.published || a.retrieved_at || "";
  return (
    `<div class="card" data-id="${esc(a.id)}">` +
    `<div class="title">${esc(a.title || a.id)}</div>` +
    `<div class="meta">${a.publisher ? `<span class="pub">${esc(a.publisher)}</span>` : ""}` +
    `<span>${esc(String(when).slice(0, 10))}</span></div></div>`
  );
}

async function loadFeed() {
  try {
    const { articles } = await api("/feed?limit=60");
    const feed = $("feed");
    feed.innerHTML = articles.length
      ? articles.map(articleCard).join("")
      : `<div class="hint">No articles yet — the driver ingests curated feeds on its cadence.</div>`;
    feed.querySelectorAll(".card").forEach((c) =>
      c.addEventListener("click", () => loadEntity(c.dataset.id)),
    );
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
      .nodeAutoColorBy("schema")
      .linkColor(() => "#2a3a52")
      .linkLabel((l) => esc(l.rel || ""))
      .onNodeClick((n) => loadEntity(n.id));
  }
  fg.width(el.clientWidth).height(el.clientHeight).graphData(data);
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

function init() {
  initGlobe();
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
