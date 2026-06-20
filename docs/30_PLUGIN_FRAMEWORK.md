# 30 — Plugin Framework (the open architecture)

> `v0.4` · June 2026 · WorldMonitor's extensibility model and L1 ingestion fabric. **Everything that
> does work is a plugin** behind a typed interface, discovered by a registry, self-describing, and
> independently enable/disable-able. This is what makes sources, algorithms, rules, and research
> **addable and removable without rewrites**. The signature surface — the **Integrations page** — is
> the catalog for the connector subset.

## 1. The plugin model

A plugin is a **self-describing unit of work**. Every plugin ships:
1. **Manifest** — id, name, **kind** (below), category, vendor, capability (`passive`/`active`),
   cost tier, **status** (`researched`→…→`operational`).
2. **Config schema** — JSON Schema for required inputs (keys, params, schedule). **It renders the UI
   form** and validates input (the Airbyte spec-drives-form pattern → zero per-plugin frontend code).
3. **Implementation** — behind the typed interface for its kind.
4. **Tests** — fixture-based (input → expected output).

The **registry** discovers plugins (entry-points / a plugins dir), aggregates manifests, and exposes
enable/disable per tenant. A plugin instance is a tenant-scoped, configured, versioned row.

## 2. Plugin kinds (the typed interfaces)

| Kind | Does | Interface (essence) | Examples |
|---|---|---|---|
| **Connector** | collect from a source | `validate · collect(cfg,state) · map → entities` | GDELT, OpenSanctions, Bluesky, Shodan |
| **Mapper** | raw → ontology (often bundled in a connector, but reusable) | `map(raw) → [FtM/STIX w/ provenance]` | STIX→FtM, GKG→`wm:Event` |
| **Resolver** | an entity-resolution strategy | `block · compare · cluster` | Splink model, nomenklatura, a name-match rule |
| **Enricher** | derive new edges/attrs on graph objects | `enrich(entity) → [entities/edges]` | passive-DNS, wallet-clustering, NER-linking |
| **Rule** | declarative trigger/policy | `when(condition) → action` | "if sanctioned entity appears in news → alert"; "if merge sensitivity > T → human review" |
| **Scorer / Algorithm** | anomaly / fusion / forecast | `score(context) → {value, calibration, provenance}` | IsolationForest, weighted fusion, fund-flow taint |
| **Notifier** | outbound delivery | `send(message, target)` | **Telegram** system alerts, webhook, email |
| **Tool** | an MCP/API tool surfaced at L8 | `run(args) → result` | query_graph, find_paths (see `60`) |

> **Research drops in as a plugin** — a new Scorer/Enricher (+ optional `wm:` ontology extension).
> Removing = unregister. Nothing else breaks because the contract is the ontology + graph.

## 3. Collection taxonomy (connectors — from OpenCTI, mandatory)

- **`EXTERNAL_IMPORT`** — passively pull on a schedule/stream (most connectors).
- **`INTERNAL_ENRICHMENT`** — triggered by a graph object to enrich it (a domain → DNS/cert; a wallet → cluster).
- **`STREAM`** — long-lived firehose consumer (Bluesky Jetstream, BGP).
- Plus **`capability: passive | active`**. **Active = gated** (authorized-scope token per run, separate
  logging, never agent-auto-run without a human).

## 4. Base classes (so most plugins reuse machinery)
`RestApiConnector` (paginated JSON) · `CliToolConnector` (containerized CLI, timeout/sandbox, egress
constrained, declares passive/active) · `StreamConnector` (WebSocket firehose, auto-reconnect + cursor
dedup; Bluesky `wantedDids` up to 10k accounts) · `FeedConnector` (RSS/OPML via feedparser + full-text
via trafilatura) · `FtmBulkConnector` (load FtM-native datasets, near-zero mapping) · `McpConnector`
(wrap an existing OSINT MCP server). Notifiers: `TelegramNotifier`, `WebhookNotifier`.

## 5. Connectors write to the contract, not the graph
Runner calls `validate` on save, then `collect → map` on schedule/stream; writes raw to the **landing
zone** (MinIO) and candidate entities to the **ER queue** (L3). A connector **never writes to the graph
directly and never resolves entities** (L3 owns that). Idempotent upserts; rate-limit + backoff.

## 6. The Integrations page (UX → architecture)

| User action | What happens |
|---|---|
| Opens **Integrations** | Catalog renders from the registry (the connector + notifier kinds), grouped by category, filterable by cost & status (available/implemented/planned). |
| **Configure** a card | Form generated from its `config.schema.json`; secrets marked; active connectors show a required **authorized-scope** field. |
| **Save** | `validate()` runs; secrets → **vault** (never the DB in plaintext); a tenant-scoped instance row is created. |
| **Enable** | Runner schedules it / starts the stream. Collection begins. |
| View instance | **Status/health:** last run, records ingested, entities produced, errors, next run. |

Forms-from-schema means **adding a connector requires zero frontend work**. Notifiers (e.g. Telegram)
configure the same way — so "send alerts to my Telegram" is a notifier instance with a bot token + chat id.

## 7. The catalog seed (your inventory becomes the backlog)
The **67-sheet OSINT Tool Inventory** seeds the catalog: each tool/source → a manifest entry tagged
`planned | available | implemented`. The spreadsheet stops being static and becomes the **living
backlog** — "building a source" = filling `collect()` + `map()` for an existing stub. Catalog metadata
is **data, not doc**.

## 8. Rules engine (declarative, pluggable)
Rules are first-class plugins: `when(condition over graph/stream) → action (alert via Notifier / set a
score / queue for review / trigger an Enricher)`. Start simple (a condition DSL over entity/edge
properties + events); rules are versioned and tenant-scoped. The self-improvement loop (`50`) may
propose new/changed rules — which go through propose→evaluate→gate→promote, never silent.

## 9. Cross-cutting rules (every plugin)
Provenance stamped at collection · secrets only in the vault (per-plugin, never logged/in-URLs) ·
hostile input parsed in isolation (no `eval`/shell-interp) · idempotent · tenant-scoped (`tenant_id`) ·
status-tagged · active capability gated.

## 10. Open decisions (need the user — see `decisions/`)
Packaging (in-repo dir vs installable entry-points — recommend dir first) · Integrations UI timing
(Phase 2) · per-plugin isolation (container for CLI/active, shared async for API/stream) · rule DSL choice.
