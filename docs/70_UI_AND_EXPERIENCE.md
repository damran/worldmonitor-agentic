# 70 — UI & Experience (the consumption surface)

> `v0.2` · 2026-07-04 · The analyst-facing design spec. Sits above L8 (API + MCP, `docs/60`) and
> consumes the resolved graph. Commissioned as the product's UI design; grounded in the strategic
> review's consumption findings (`docs/fable-review/50_FABLE_REVIEW.md` F4) and the operator's
> answers in ADR 0094 (persona = CTI / L3 SOC; OSS-only; no paid products; ~5 h/week review budget)
> and ADR 0095 (Postgres statement-log = system of record; Neo4j = projection).
>
> **Status:** design spec — nothing here is built yet. The existing UI is the Integrations page
> (ADR 0069). This document defines the target and the build order; it is a plan, not a claim.

---

## 1. Principles (what every screen obeys)

1. **The product is answers with receipts, not a graph.** The consumable unit is the alert, the
   diff, the dossier, the lead — each tracing to an addressable, sourced claim. The graph is the
   asset that makes answers trustworthy; it is *a view*, not the front door.
2. **Overlays are the founding motivation and the spine.** A toggleable, time-aware,
   provenance-carrying data layer — but generalised (§2) into one primitive that renders across map,
   graph, dashboard, and alert. This is what makes "WorldMonitor" a monitor.
3. **Ontology-driven — the UI is generated, not hand-built.** Entity views, overlay legends, and
   widgets render from the FtM/STIX/`wm:` schema + plugin manifests. A new domain (CTI → pandemic →
   markets → conflict) is a **schema + plugin pack, not a frontend rewrite** (§6). This is the
   operator's explicit extensibility mandate, realised as a rendering architecture.
4. **Provenance is always visible.** Every rendered fact carries a **source chip** (source ·
   reliability · retrieved-at · raw pointer). This is the C3 invariant made visual, and the audit
   log made legible.
5. **Leads, not verdicts (C5), enforced at render time.** Risk/attribution render as **ranked
   hypotheses with a confidence band** — there is no "verdict" component in the design system.
6. **The review queue is a first-class surface** (safety control + calibration label factory),
   sized to the ~5 h/week budget (ADR 0094 D6).
7. **Everything is tagged, and tags are searchable** (§6.1, ADR 0096). A tag is a provenance-carrying
   claim, so tag-search, tag facets, and tag-driven overlays/alerts are a first-class retrieval layer —
   one of the platform's main points, not an afterthought.
7. **Modern but simple, and extensible by construction.** Server-rendered HTMX shell + a few
   self-contained interactive islands (§9). No SPA. OSS-only, CSP-strict, self-hosted (including the
   basemap — no external tile calls). A solo operator must be able to hold the whole thing in their
   head, and a plugin must be able to add UI without touching the frontend.

---

## 2. The one primitive: the **Overlay** (a Lens with render adapters)

Everything toggleable in WorldMonitor is the **same object**. Call it an **Overlay** (its founding
name) or a **Lens** (its general name):

> **Overlay = a saved, parameterised, time-aware *selection* over the resolved graph + a render
> spec.** It carries: an **ontology binding** (which FtM/STIX/`wm:` types & properties), a **time
> field** (`valid_from`/`valid_to` world-time, or `asserted_at` belief-time — free once ADR 0095's
> bitemporal statement log exists), a **geometry resolver** (how a result gets placed: a coordinate
> property, a GeoNames/ISO-3166 region, or an aggregate), a **style** (ramp keyed to a property), a
> **confidence predicate**, and **provenance pass-through**.

A **renderer registry keyed on result shape** renders one Overlay four ways:

| Result shape | Renderer | Surface |
|---|---|---|
| geometry | map layer (MapLibre/deck.gl) | **Monitor** |
| node/edge predicate | graph filter/tint | **Investigate → Graph** |
| scalar / rows | dashboard widget (count, sparkline, table) | **Dashboards** |
| predicate + schedule | watchlist / diff alert | **Telegram / Ask briefings** |

**Overlay = graph filter = widget = watchlist — the same object, different renderer.** That is how
overlays are first-class *and* cross-surface, and why a new capability is a Lens, not a new screen.

**The honest caveat (adopt the fix up front).** A single object cannot cleanly be *both* a map
overlay (wants geometry + aggregation + time-binning) *and* a graph filter (wants type/edge
predicates + traversal depth) without becoming a leaky union. So the internal shape is a **shared
`Selection` core** (the query + ontology binding + time + confidence) plus **per-surface render
adapters** (`MapAdapter`, `GraphAdapter`, `WidgetAdapter`, `AlertAdapter`). The user sees one
Overlay; the code has one selection and N adapters. Treat "reuse as graph filter" as *adapting* the
selection, not reusing an identical render object. This keeps the cross-surface promise real instead
of ~80% marketing.

---

## 3. Information architecture

**Workbench-first, with a configurable home and Monitor as a first-class top-level mode.** The
persona (CTI / L3 SOC analysts, ADR 0094 D4) lives a *triage → investigate → sign-off* loop, and the
review + all three clean-slate architects ordered the build review-queue → watchlist → dossier →
explorer, map last. So the daily driver is the **Desk**, not a spinning globe. But the **founding
motivation is the map/overlays**, and the Overlay primitive (§2) is exactly what lets the map be
first-class without being the mandatory front door. Resolution: **the default landing surface is
operator-configurable** (Desk / Monitor / a saved Dashboard).

**Persistent left rail (fixed surfaces) + a workspace of user-created pages:**

| Surface | Role | Reached |
|---|---|---|
| **Desk** (default home) | Global entity search, my cases, **review-queue badge**, watchlist diffs, recent Overlays, agent activity. | default |
| **Cases** | The investigation workbench (§4B). The daily driver. | rail / from a case |
| **Monitor** | Map-forward situational mode: all domains' overlays, global (§4A). The "world monitor". | rail / configurable home |
| **Review** | ER sign-off queue — safety control + label factory (§4D). | rail / alert badge |
| **Store** | Enrichment marketplace: connectors, enrichers, overlays, domain packs, scorers, notifiers, tools (§7). | rail |
| **Library** | Reusable objects: Overlays, **Views**, dashboards, saved queries. | rail |
| **Ask** | Hermes console; also **side-loadable** as a docked panel on any surface. | rail / right dock |
| *Workspace pages* | User-created **Dashboards**, **Query workbenches**, **Chat consoles**. | "＋ New page" |

**Cross-surface chrome:** a global **bitemporal time control** (one cursor shared by map/graph/
widgets; world↔belief toggle; "diff since t"; play), a collapsible **right dock** (Hermes chat +
provenance inspector), and **⌘K** (search / run a saved query / jump / *ask Hermes*).

---

## 4. Key screens

### 4A. Monitor — the world map + overlays (the founding feature)

Full-bleed **MapLibre GL JS** (BSD-3) over a **self-hosted Protomaps PMTiles basemap** served from
MinIO by HTTP range requests — **no external tile CDN**; sovereignty is per-workload with a
local-default posture (ADR 0094 D2). **deck.gl** (MIT) interleaves GPU overlays (arcs for
flows/attribution, hexbin/heat aggregation, large point sets) via `MapboxOverlay`.

- **Left — Overlay stack** (Photoshop-style layer list, grouped by domain pack): each row = toggle ·
  opacity · legend swatch · time-binding indicator · **provenance badge (source count)** · confidence
  slider. `+ Add layer` from a Library Overlay, a live query, or an installed enrichment/STREAM
  connector. The stack *is* the overlay registry (MapLibre's `layers[]` with stable ids; toggle =
  `setLayoutProperty(id,'visibility',…)`).
- **Center — the map:** clustered points / choropleth / heat / relationship arcs. Click a feature →
  a **provenance-first popover**: source chips, `retrieved_at`, reliability, a **confidence band
  (never a verdict)**, and actions *Pivot to Investigate · Pin to case · Add to watchlist · Ask
  Hermes*.
- **Right — alert ticker** (the watchlist/diff feed) + the Hermes dock.
- **Bottom — the global time scrubber:** scrub → every time-aware overlay re-queries; **diff mode**
  paints additions/retractions between two instants (free from the bitemporal log). Lasso a region →
  *Create case from selection* / *New Overlay from selection*.

### 4B. Investigation workbench — the daily driver (Case + entity + graph)

Three panes over **one selection**; the time + overlay context follows the analyst.

- **Left — Case rail:** the case's entities, attached Overlays, evidence/notes, tasks, watchlists,
  and a case-event timeline. (A **case** is the analyst's unit of work — a scoped collection, not a
  tenant; ADR 0094 D3 keeps this single-tenant.)
- **Center — tabbed canvas over the same selection:** **Dossier · Graph · Map · Table · Timeline**
  (the switcher preserves selection + time cursor).
  - **Dossier (ontology-driven):** canonical-anchor chips (Wikidata Q / LEI / GeoNames / ISO-3166); a
    property sheet **generated from the FtM/STIX/`wm:` schema** (no per-type template); a **claim
    timeline** (each claim = row + source chip + `asserted_at` + reliability); a **contradictions
    block** (competing values for one property — trivial in a statement store, near-impossible in
    collapsed graph properties); a **Leads** section (ranked hypotheses + confidence, never a verdict).
  - **Graph:** **Cytoscape.js** (MIT) over the hop-capped `neighbors`/`find_paths` API — chosen for
    the investigator's interaction needs (expand/collapse, context menus, path-finding, and
    **provenance styling**: reliability/source → border/edge-dash via style selectors). Merge
    candidates render as dashed `possibly_same_as` links → *Send to Review*. **Scale escape hatch:**
    swap to **sigma.js + Graphology** (WebGL) above ~3–5k nodes, where Cytoscape's canvas layout
    blocks the main thread; investigator subgraphs are usually hundreds of nodes, so interaction
    quality wins by default.
  - **Map / Table / Timeline:** the same selection through the other render adapters.
- **Right — Inspector:** the selected entity's schema-driven property sheet, per-claim source chips,
  contradictions, confidence, and **Run enrichment** buttons (each output becomes an Overlay attached
  to the case).

### 4C. Store — the enrichment marketplace (§7 details the model)

Catalog grid grouped by **kind** and **domain pack**; filters for domain · kind · mode · capability
(active shows the gated-token warning) · status tag · **OSS-license badge**. Detail page tabs
render from the manifest: **What it is · How to use · How to connect · Config · Provenance &
License**. **Install → Configure** reuses the existing `config.schema.json` form renderer (ADR 0069)
verbatim. A **Pack** is a bundle card (schema + plugins + default Overlays + dashboard) → *Install
pack* lights up a whole domain (§6).

### 4D. Review queue — safety control + label factory

Side-by-side candidate entity cards, **statement-level evidence diff**, the guard reason + a
confidence band, and one-keystroke **approve / reject / split / abstain**. Sensitive merges show a
prominent **"blocked pending human sign-off"** badge (block-default is already the code default —
`settings.py:83`, ADR 0031). Every verdict lands as a gold label. This is where the ~5 h/week budget
and the abstention band are spent; when queue debt exceeds the budget, the ER thresholds tighten
automatically (degrade-conservative — review F3/§3.7), never accrue into a silent auto-approve.

### 4E. Dashboards, Query workbench, Ask (workspace pages)

- **Dashboards:** a **gridstack.js** (MIT, zero-dep) drag/resize widget grid; each widget = an
  Overlay + a chosen renderer. `grid.save()`/`load()` serialises layout to JSON in Postgres; share by
  id. "Build a dashboard" = pick an Overlay, pick a renderer, resize.
- **Query workbench (§8):** a **CodeMirror 6** editor (native Cypher + the friendly pipe layer) →
  results as table / graph / map, with **Promote to Overlay** and **Save as alert** (query + schedule
  + threshold → Telegram).
- **Ask (Hermes console):** the chat surface, standable as a page *and* side-loadable as a right-dock
  panel on any surface (the S5 operator console, `docs/50`/ADR 0089) — server-rendered chat page in
  the FastAPI/Jinja app streaming Hermes' answers, never a separate SPA. Chat routes through Hermes so
  it gets the agent (our MCP tools + skills); Hermes' model routes through the LiteLLM gateway so the
  sovereignty posture holds.

---

## 5. The Overlay lifecycle

1. **Create** — from a promoted query result, a **tag or tag-query** (§6.1), an enrichment/connector
   output, a saved selection on any surface, an alert/watchlist, or a manual definition. Mappability is
   *inferred from the schema* (any geo property or GeoNames/ISO-3166 anchor makes it map-able).
2. **Bind** — source selection · ontology type(s) · geometry resolver · **time accessor** · style
   ramp · confidence predicate · provenance pass-through.
3. **Toggle** — it enters the **Library** and is available on every surface.
4. **Time-scrub** — the global bitemporal cursor filters claims on the chosen axis; diff mode
   animates deltas.
5. **Save into a View** — a **View** = `{basemap, ordered overlay stack, time window, camera, scope}`,
   a shareable board that a Monitor tab or a dashboard loads.
6. **Reuse** — the same selection, adapted (§2), onto the **graph** (filter/highlight), a
   **dashboard** (widget), or a **watchlist** (diff alert via TelegramNotifier / a Hermes brief).

One definition → four surfaces, via the shared `Selection` + render adapters.

---

## 6. Ontology-driven rendering — a new domain is a pack, not a rewrite

This is the operator's extensibility mandate (**CTI → pandemics → markets → conflict**) realised as
two registries:

- **Schema→widget registry** (extends ADR 0069's config-form renderer to *entity views and legends*):
  an FtM datatype → a widget. `string`→text; `date`→timeline chip; `entity`→link chip;
  `country`→flag + ISO chip; `identifier`→canonical-anchor chip; `coordinate`→map pin;
  `number`/quantitative→sparkline. A `wm:` extension registers a widget for a novel datatype **once**.
- **Renderer registry** (§2): result shape → surface renderer.

Together they mean a domain pack ships **only data artifacts** — schema YAML + plugin manifests +
mappers + declared default Overlays + an optional dashboard template — and the UI lights up with
**zero frontend code**.

**Worked example — a Pandemic pack** (proof the mandate holds beyond CTI):
1. Schema: `wm:Pathogen`, `wm:Outbreak`, `wm:CaseCount` (a quantitative claim bound to a GeoNames
   region + time), validated against the FtM contract.
2. Connectors (WHO / ECDC / open genomic feeds, pull-only) + mappers emit those types **with
   provenance**.
3. Install → the `config.schema.json` form is auto-generated (ADR 0069). The registry gains
   `wm:Outbreak` → the **Dossier renders it from schema**, source chips and all, with no template.
4. Declared default Overlays appear under a new "Pandemic" layer group → incidence choropleth on
   ISO-3166, scrubbable by report date; a variant-sighting point layer; an R-number timeseries widget.
5. The same Overlays are graph filters (`Outbreak—affects→Region`, `Pathogen—variantOf→Pathogen`) and
   dashboard widgets.
6. Central Splink ER dedups `wm:Pathogen`/`wm:Outbreak` candidates through the *same* Review queue;
   Hermes reads them through the *same* MCP tools — no new tool code.

**The other three domains fit the same mechanism:** **CTI** is the default persona (STIX SDOs/SCOs →
`wm:Domain`/`wm:IPAddress`/`wm:Certificate`, largely ageospatial — the graph and dossier are home,
the map is a thin lens); **conflict reporting** = ACLED/CAMEO events as `wm:Event` (geo + time native
— the map/overlay surface shines); **market investments** = `wm:Instrument`/`wm:Position`/
`wm:MarketSignal` as scored, calibrated leads (dashboard + timeseries home). The UI never changes;
the pack does. (Ontology governance stays as `docs/20` §4/§9: additive `wm:` schemas, ADR-per-
extension.)

### 6.1 Tagging & labels — the searchable retrieval layer (ADR 0096)

Tagging is a **first-class** capability, not a bolt-on — and it falls straight out of the storage
model: **a tag is a provenance-carrying statement** (`ADR 0096`). So every tag records *who/what
applied it, when, and how confidently*, is bitemporal (when-tagged / retracted / valid-for), is
erasable and auditable like any claim, and — the point the operator cares about — is **searchable
through the same surfaces as everything else**.

- **Four kinds, one mechanism:** *ontology labels* (STIX `labels`, FtM topics, sensitivity), *analyst
  tags* (free or controlled, applied during investigation), *auto-tags* (from scorers/rules — they
  carry a **confidence** and render as leads, never verdicts; a person-affecting auto-tag is
  **sign-off-gated** exactly like an ER merge), and *system tags* (reliability, source, status).
- **Controlled taxonomies:** namespaced vocabularies — `tlp:amber`, `admiralty:reliability=B`,
  `misp-galaxy:threat-actor="…"`, STIX `labels` — are first-class; a taxonomy is a small ontology
  extension (data, not code). Free tags are allowed but the UI nudges toward controlled vocab so
  search stays reliable.
- **UI:** **tag chips** wherever an entity/claim/case renders (coloured by namespace; the source chip
  on hover; a confidence dot on auto-tags); a **faceted tag search** on the Desk (namespace → values →
  counts — the fast "tag search"); **bulk-tag** a selection in the workbench, the graph, or a query
  result; a **non-destructive tag manager** (rename/merge/deprecate = supersede, provenance kept).
- **Tag = Selection = Overlay:** a tag or tag-query is a §2 Selection — *promote it to an Overlay* and
  "everything tagged `sanctioned` in the last 30 days" renders on the map, as a graph filter, a
  dashboard widget, or a **tag-driven watchlist alert**. Tags are queryable in the pipe-DSL
  (`search tag:sanctioned`) and Cypher, and exposed via the read API/MCP so **Hermes can filter by
  tag** (read now; *applying* a tag from the agent is a Phase-6 gated write tool).

---

## 7. The marketplace + the plugin UI-contribution model

WorldMonitor already renders config from each plugin's `config.schema.json`, so the marketplace is a
**thin layer**: a catalog index built by scanning manifests (no per-plugin frontend) + a small set of
**manifest additions** + a two-step **Install → Connect** flow. Pattern drawn from Grafana's plugin
catalog + UI-extensions, VS Code contribution points, Obsidian/HACS, and n8n's verified-node trust
tiers.

**Manifest additions (all optional; the config form is still `config.schema.json`):**

```jsonc
{
  "id": "opensanctions-connector",
  "listing": {                          // marketplace card + detail page
    "title": "OpenSanctions", "summary": "Sanctioned & PEP entities as FtM.",
    "category": "sanctions", "tags": ["ftm","canonical-id"],
    "icon": "img/icon.svg", "screenshots": ["img/1.png"],
    "docs": { "about": "docs/about.md", "usage": "docs/usage.md", "connect": "docs/connect.md" },
    "maturity": "operational",          // reuses WM's researched→…→operational tags
    "version": "1.2.0", "min_wm_version": "0.9", "license": "MIT", "source": "https://…"
  },
  "connect": {                          // install → connect (distinct steps)
    "auth": "apikey",                   // none | apikey | oauth2
    "secrets": ["api_key"],             // x-secret → vault, masked, never echoed
    "test_endpoint": "/plugins/opensanctions/health"
  },
  "contributes": {                      // UI slots (declarative — no frontend edit)
    "overlays": [{ "id":"os-sanctioned-vessels", "title":"Sanctioned vessels",
      "data":"/plugins/opensanctions/overlay/vessels.geojson", "geometry":"point",
      "time_field":"retrieved_at", "legend":{"kind":"categorical","field":"program"},
      "provenance_fields":["source_id","reliability","retrieved_at"] }],
    "widgets": [{ "id":"os-counts", "title":"Sanctions by program",
      "data":"/plugins/opensanctions/widget/counts", "viz":"bar" }],
    "panels":  [{ "id":"os-entity", "title":"Sanctions record",
      "selector":"stix:identity|wm:vessel", "data":"/plugins/opensanctions/panel/{entity_id}" }],
    "functions":[{ "id":"os-lookup", "signature":"lookup(entity_id) -> record" }]
  }
}
```

Every contribution names a **namespaced data endpoint** on the plugin's own FastAPI router + a
**render spec the host interprets** — so adding a plugin adds UI with **zero frontend edits**, and no
third-party JavaScript enters the main page.

**The render contract — three escalating tiers, prefer the lowest (the security spine of extensibility):**

- **Tier 0 — declarative data + spec (default, safest).** The plugin returns **data only** (GeoJSON /
  JSON rows) + a render spec (legend, geometry, time field, viz). The **host** draws it with its own
  trusted MapLibre/deck.gl/Cytoscape/chart code. **Zero third-party code executes.** Covers overlays,
  widgets, and most panels — the Grafana-panel / Kepler-layer model.
- **Tier 1 — server-rendered HTMX fragment.** For a custom entity-detail panel, the plugin's router
  returns a **sanitised HTML partial** via `hx-get` under a namespaced path; strict CSP, HTML
  sanitisation, all plugin output treated as hostile (the existing rule). No JS ships.
- **Tier 2 — sandboxed iframe (escape hatch, gated).** Only when bespoke interactive JS is
  unavoidable: a `sandbox`ed iframe on a separate origin, strict CSP + per-load nonce, communication
  **only via `postMessage`** to a narrow host-brokered API — never direct DOM or token access. Tier 2
  is an **active/gated capability** (authorised per install, separately logged, never agent-auto-
  enabled) — consistent with the active-plugin gating in `docs/10` §6.

---

## 8. The query surface — two layers + an API layer

The operator is most fluent in **Sumo Logic pipe syntax** (`search | parse | where | stats … by |
sort`). No single language covers pipe-filter/aggregate over the Postgres statement log **and** graph
traversal, so the design is layered (as Aleph, Grafana, and Kusto all split it):

1. **Power-user / graph core = native Cypher on Neo4j Community.** Free, native, ISO-GQL-tracked, the
   genuinely-more-powerful-than-Sumo answer for the graph core (neighbors / paths / patterns). Editor
   = the official **neo4j/cypher-editor** (CodeMirror 6, Apache-2.0, schema-aware autocomplete). Ships
   **first**, near-zero cost, behind the trusted/admin role (raw Cypher stays admin-gated per review
   F11; untrusted callers get parameterised templates).
2. **Friendly pipe layer = the Sumo-familiar surface** over the Postgres statement log. This is a
   genuine fork the operator should decide (Open Question Q3), because each option trades familiarity
   against maintenance:
   - **(a) WM-QL — a thin custom pipe-DSL** cloning a named subset of OpenSearch PPL / SPL commands
     (`search | where | stats … by | sort | eval | parse`), parsed with Lark/Lezer → **parameterised
     SQL** over the statement log. *Instantly familiar* to a Sumo user; pipe-over-SQL is a
     legitimised pattern (BigQuery pipe syntax). Cost: you own a parser + codegen + CodeMirror grammar
     + docs — the "custom-DSL uncanny valley" risk for a solo maintainer.
   - **(b) PRQL** (Apache-2.0, "never commercial", compiles to SQL, Python bindings): *zero language
     to maintain*, but **FROM-first analytics ergonomics, not search-first** — a Sumo user won't feel
     instantly at home, and it's SQL-only (no graph).
   - **(c) Cypher + saved parameterised templates only** — skip the pipe layer entirely; cheapest,
     least familiar.
3. **API layer = GraphQL → Cypher** (Neo4j GraphQL Library or Strawberry/Ariadne with Cypher
   resolvers) for programmatic/UI fetch — never the human query bar.

Both layers include **tag search** as a first-class filter (§6.1): `search tag:sanctioned` in the pipe
layer, a tag predicate in Cypher, and a faceted tag sidebar on the Desk — the fast tag-retrieval the
operator asked for, backed by a rebuildable tag index.

**Handoff:** pipe/Cypher result rows carry canonical entity IDs → *Open in graph* pivots to a Cypher
view; *Promote to Overlay* turns any result (including a **tag query**) into the §2 primitive; *Save as
alert* wraps either language with a schedule. **My recommendation:** ship Cypher + templates now (covers the power need at
near-zero cost); decide the pipe layer (a/b/c) when the query workbench is actually built — leaning
(a) WM-QL *if* you'll value the Sumo familiarity enough to maintain a small grammar, else (b) PRQL.

---

## 9. Frontend architecture & stack (HTMX shell + interactive islands, OSS-only)

**Stay HTMX-first; do not adopt a SPA** (honours ADR 0069 and single-operator maintainability). The
four rich surfaces are inherently client-stateful WebGL/canvas widgets; the standard 2025 pattern is
**islands** — server-rendered HTMX/Jinja pages that mount a few self-contained custom-element widgets,
each owning its state and talking to the API directly (JSON), not via `hx-*`.

| Layer | Pick | License |
|---|---|---|
| Pages / nav / CRUD / forms | **htmx v2** | 0BSD |
| Micro-reactivity (toggles, tabs, chat input, overlay checkboxes) | **Alpine.js v3** | MIT |
| Island base (optional, for plugin-contributed UI) | **Lit v3** | BSD-3 |
| Base map | **MapLibre GL JS** | BSD-3 |
| Self-hosted basemap tiles | **PMTiles + Protomaps** (from MinIO) | BSD-3 code / CC0 tiles |
| GPU map overlays (lazy) | **deck.gl** | MIT |
| Graph explorer | **Cytoscape.js** (+ **sigma.js/Graphology** at scale) | MIT / MIT |
| Dashboard grid | **gridstack.js v12** | MIT |
| Query editor | **CodeMirror 6** (+ neo4j/cypher-editor; Lezer grammar for WM-QL) | MIT / Apache-2.0 |
| Charts (widgets) | **uPlot / Observable Plot** | MIT |

**Two hard constraints for whoever builds this:**
- **Island state-loss hazard:** an `hx-swap` that replaces a subtree tears down any island/Alpine
  state inside it. **Rule: island mount points live *outside* any `hx-swap` target;** drive island
  *data* via its own fetch; clean up with `htmx:afterSwap` + `disconnectedCallback`.
- **CSP is strict-but-not-trivial:** MapLibre/deck.gl need `worker-src`/`blob:`; CodeMirror needs a
  CSP **nonce** for its injected `<style>`. All are CSP-compatible (the reason Monaco was rejected —
  it effectively forces `unsafe-eval`). Bundle the islands with esbuild/vite for hashing + SRI.

**Extensibility fit:** the config/marketplace forms render from `config.schema.json` inside an
Alpine/Lit island (same seam as ADR 0069). A plugin needing a bespoke widget registers a custom
element in its manifest + ships one ES module mounted by tag (Tier 2, gated) — preserving "zero
per-plugin frontend code" by default while allowing opt-in rich UI, under one no-SPA, strict-CSP
architecture.

---

## 10. Design system — the cross-cutting atoms

Used on every surface, so the invariants are visual, not aspirational:

- **Source chip** — icon + reliability letter + `retrieved_at` + raw-pointer link. On **every** fact
  (C3 made visual).
- **Confidence band** — a gradient with a hover distribution. There is **no verdict component** (C5).
- **Canonical-anchor chip** — Wikidata Q / LEI / GeoNames / ISO-3166; monospace id, links out.
- **Tag chip** (§6.1) — namespace-coloured; source chip on hover; a confidence dot on auto-tags.
  Click a tag → faceted tag search; drag a tag → promote to an overlay.
- **Ontology-type badge** — colours by domain pack (pack identity = accent).
- **Overlay pill** — toggle + time-binding + provenance count; draggable across surfaces.
- **Bitemporal time control** — one global cursor; world↔belief toggle; diff-since-t; play.
- **Status tag** — `researched → … → operational` (reused from plugin manifests).
- **Gate / sensitivity badge** — "blocked pending human sign-off"; the merge-guard/active-tool state.

**Tone:** neutral dark-first, cartographic; one restrained accent per domain pack; dense-but-calm;
keyboard-first (arrow-scrub time, one-key review verdicts, ⌘K everywhere).

---

## 11. Build sequencing (how this lands incrementally)

> The authoritative, cross-cutting build order (storage + ER + tagging + UI + operator runbook) lives
> in the **execution handoff plan** (`docs/fable-review/70_EXECUTION_HANDOFF.md`). The UI-only order
> below is the slice of it that concerns this document.

Trails the storage/ER work (it needs claims + a review queue to be worth building), and every step
is a self-contained slice that fits the existing HTMX app.

1. **Review-queue UI** (§4D) — promotes the sign-off CLI; runs in parallel with the F1 statement
   spine; no dependency on it. First, because it is the safety control *and* the label factory.
2. **Entity dossier + Cytoscape graph** (§4B center) over the existing hop-capped API — the schema→
   widget registry starts here.
3. **Overlay primitive + Monitor map** (§2, §4A) — MapLibre + PMTiles + deck.gl; the founding
   surface; watchlists/diffs become alerts.
4. **Marketplace listing + `contributes` Tier 0** (§7) — turns the Integrations page into the Store;
   plugins start contributing overlays/widgets declaratively.
5. **Dashboards + Query workbench** (§4E, §8) — gridstack + CodeMirror + Cypher; the pipe-layer
   decision (Q3) resolves here.
6. **Ask console** (§4E) — the S5 Hermes chat surface, dockable everywhere.

Domain packs (pandemic / conflict / markets) become buildable once §2 + §6 exist — each is a pack,
not a UI project.

---

## 12. Open questions (UI-specific — for the operator)

> Answer these and I'll fold them into a v0.2. They are the choices the design can't make for you.

1. **Default landing surface** — Desk (workbench, my recommendation for the CTI persona), Monitor
   (map-forward, the founding feel), or a saved Dashboard? (The design supports all three as a config;
   this only sets the default.)
2. **Graph explorer scale** — is the investigator's typical working subgraph in the hundreds of nodes
   (→ Cytoscape.js, richer interaction, my default) or routinely thousands-plus (→ sigma.js/Graphology
   from the start, WebGL, thinner interaction)? Sets which is primary.
3. **The pipe query language** — (a) WM-QL, a thin custom Sumo-like pipe-DSL you'd maintain
   (most familiar, ongoing cost); (b) PRQL, zero-maintenance but FROM-first/less-Sumo-like; or
   (c) Cypher + saved templates only, no pipe layer? Cypher ships first regardless; this is only about
   the friendly layer on top.
4. **Basemap cartography** — self-hosted Protomaps planet PMTiles (~120 GB on MinIO, fully sovereign,
   my recommendation) vs a lighter regional/coarse basemap to save storage? Any preference on map
   style (dark/satellite-like/minimal)?
5. **Plugin UI trust ceiling** — cap third-party contributions at **Tier 1** (declarative + sanitised
   HTML fragments, no plugin JS ever), or allow **Tier 2** (gated sandboxed-iframe islands) for
   community packs that need bespoke interactivity? (Tier 0/1 cover the built-in packs; Tier 2 only
   matters if you expect community-authored rich widgets.)
6. **Chat scope** — should the Ask console expose only read/investigation (its current MCP surface),
   or eventually run enrichments / start investigations from chat (a Phase-6 write-tool question the
   UI should anticipate but not enable yet)?
7. **Dashboards/Views sharing** — since there's no multi-tenancy (ADR 0094 D3), are dashboards/Views
   purely personal, or do you want export/import (JSON) so a domain pack can *ship* a starter
   dashboard? (Recommend the latter — it makes packs feel complete.)

---

*Evidence: mid-2026 landscape research on the query language, frontend architecture, and
marketplace/overlay/extensibility patterns, each adversarially fact-checked (all core library/license
claims confirmed; MapLibre = BSD-3, PMTiles/Protomaps = BSD-3+CC0, Cytoscape/deck.gl/gridstack = MIT,
CodeMirror-over-Monaco for CSP). Two independent information-architecture explorations (map-first and
workbench-first) converged on the single-primitive + islands + ontology-driven-rendering spine; their
one genuine disagreement (which surface is "home") is resolved as a configurable default.*
