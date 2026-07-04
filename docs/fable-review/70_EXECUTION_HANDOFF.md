# 70 — Execution Handoff Plan (Fable → Opus)

> **What this is.** The planning phase (Fable 5) is done. This is the **build brief for a fresh Opus
> session** to execute via the existing gate fleet. Fable did not build anything; every decision below
> is recorded (ADRs 0094/0095/0096 + the review) and here it is turned into an **ordered, cold-startable
> gate sequence** with acceptance criteria, owning ADRs, dependencies, and an **operator runbook** for
> the tasks only the human can do.
>
> **How to use it (Opus).** Run gates in order unless a dependency note says a gate can parallelise.
> Each gate is a normal fleet gate (orient → plan → test-author → builder → checker → judge →
> self-merge on green). Property/metamorphic tests remain mandatory for any gate touching an invariant
> (ER/merge/provenance/canonical-ID/erasure/**tagging-of-a-person**). The operator (Mithat) runs only
> the runbook tasks (§Operator runbook); everything else is the fleet's.

---

## 0. Confirmed decisions (the settled context — do not relitigate)

From ADR 0094 (operator answers), ADR 0095 (storage), ADR 0096 (tagging), and the 2026-07-04
questionnaire answers:

- **No building by Fable; Fable plans, Opus executes per this plan.** Build-related choices are
  relegated here, not decided in chat.
- **Commercial:** none / open-core only. **Public flip = reports only** — the *software/repo stays
  private*; only the **reports/briefings the operator and Hermes generate** are made public. So a
  public code release is **not** on the horizon → the AGPL-LICENSE-at-public-flip is **deferred**
  (repo stays private); the compliance surface shifts to *published reports* (see §Comms/compliance).
- **Storage (ADR 0095):** Postgres statement+decision log = system of record; Neo4j = derived,
  rebuildable projection. DR = **rebuild-from-Postgres** (confirmed — Neo4j Community forever, no paid
  products).
- **Tagging (ADR 0096):** first-class, tag = provenance-carrying statement; controlled taxonomies;
  auto-tags are leads (person-affecting ones sign-off-gated); tag-search + tag-as-overlay.
- **Persona:** CTI / L3 SOC analysts. **Domain-pack order:** CTI first (Phase-4 enricher), **conflict
  reporting second**, then markets / pandemic. `wm:` naming + `wm:Place` decided **per-pack at
  pack-spec time**.
- **ER scale:** build-deeper-never-buy; Splink/nomenklatura forever; no Senzing. **Review budget
  ≤5 h/week** → abstention band + sampling sized to it, **degrade-conservative** mandatory.
- **Sovereignty:** zero-egress *claim* dropped; per-workload policy; local default stays;
  claude-headless retained until Anthropic breaks/prohibits it. **Local boundary-annotation model =
  Qwen3-4B-Instruct (Q4_K_M/Q5_K_M, Ollama)** on the operator's 6 GB GPU (§Operator runbook).
- **Governance:** retire `GATE_LEDGER.md`; add reversibility-classification verification to the
  checker/judge mandate; add the FtM version-pinning ADR.
- **UI (docs/70):** all §12 answers = "looking good" → defaults stand (Desk default home; Cytoscape
  primary + sigma.js at scale; PMTiles self-hosted basemap; export/import dashboards). **Pipe query
  language (D3)** relegated to Gate 9 (see §Relegated micro-decisions).

---

## 1. The gate sequence

Legend: **[deps]** dependencies · **[ADR]** owning decision · **[human]** needs an operator runbook task.

### Gate 0 — Truth-up & governance *(docs + CI only; run first)*
- **Scope (F5 + F1/F2 governance):** regenerate the ADR index (currently stops at 0035; automate from
  headers); flip the ~20 merged-but-PROPOSED ADRs to ACCEPTED; back-annotate 0024→0031 + fix 0031's
  two broken links; **retire `GATE_LEDGER.md`** (roadmap + ADR headers carry state); purge stale
  `tenant_id` and **zero-egress identity** language (docs/20 §2, docs/40 Phase 0, decisions/README
  rows 14/31, and anywhere "data never leaves the perimeter" reads as identity); update **docs/20 §2.3**
  ("Store = Neo4j" → "Store = statement log (Postgres, SoR) → projected to Neo4j", ADR 0095); **drop
  `wm:Article`** (keep FtM `Article`); record shipped Phase-3 slices S1–S3b in the roadmap (leave the
  operational Phase-3 checkboxes unticked until the deploy lands); mark `ARCHITECTURE_REVIEW.md` a dated
  snapshot. Add **FtM version-pinning ADR** (pin exact version, vendor schema YAMLs as data, upgrade
  cadence + a schema-diff CI gate). Add **"verify the ADR's reversibility & person-affecting tags
  against the diff"** to the checker/judge mandate + a human co-sign line on any ADR self-tagging
  non-sensitive or waiving a human fork.
- **Acceptance:** docs match the repo; index complete; no PROPOSED-but-merged ADRs; GATE_LEDGER gone;
  FtM pinned with a passing schema-diff gate; checker/judge spec updated. **[ADR: 0088-style CI + new
  FtM-pin ADR]**

### Gate 1 — Review-queue UI *(parallel with Gate 2)*
- **Scope (review F4.1):** promote the sign-off CLI (`worldmonitor.review`) to a server-rendered HTMX
  web UI — side-by-side candidate cards, statement-level evidence diff, one-keystroke approve / reject /
  split / abstain; the **blocked-pending-sign-off** badge (block is already the default); every verdict
  written as a gold label. Keyboard-first.
- **Acceptance:** an operator can clear the parked-merge queue in the browser; each verdict lands in the
  gold-label store; sensitive merges show the block badge. **[deps: none — no dependency on the storage
  spine] [ADR: 0031/0047 unchanged]**

### Gate 2 — Statement spine, steps 1–2 *(ADR 0095)*
- **Scope:** create the statement + decision tables (with a `scope`/`workspace` default-`'default'`
  column reserved though tenancy = none); **dual-write** the fused `StatementEntity` at merge time
  (already built in memory — persist it); **backfill** — run a *fidelity spike first* (single-source
  nodes exact; merged nodes from `prov_witnesses`; pre-0045 merges possibly lossy) and record the
  per-cohort choice in the ADR. **No user-facing change.**
- **Acceptance:** every new merge writes statements + a decision row; backfill fidelity documented;
  Neo4j remains the live SoR (no cutover yet). **[deps: none] [ADR: 0095 steps 1–2]**

### Gate 3 — Projector + rebuild-and-diff → cutover *(ADR 0095 steps 3–5)*
- **Scope:** transactional outbox → idempotent Neo4j projector (idempotent MERGE on canonical id) +
  Postgres checkpoint; a **scheduled full-rebuild-and-diff job** (the fold-determinism guard — **pages
  the operator on divergence**); cut the graph writer over to the projector; retire the direct write
  path; the projection gets **no write path except the projector**. This is the DR story
  (rebuild-from-Postgres) made real.
- **Acceptance:** Neo4j is rebuilt solely by the projector; the rebuild-and-diff job runs green and is
  wired to alert on divergence; a from-scratch rebuild reproduces the graph; dual-write hazard gone.
  **Property tests: fold determinism.** **[deps: Gate 2] [ADR: 0095 steps 3–5]**

### Gate 4 — Incremental ER + calibration *(review F2 + F3; person-affecting)*
- **Scope:** persist the resolved corpus as a linkable index; micro-batch `find_matches_to_new_records`
  + new-vs-new within window; append judgements to the durable resolver log; keep the periodic **full
  re-cluster** as corrective truth; wire dormant **pgvector** as ANN candidate blocking for
  multilingual names. **Calibration:** mixture-model FDR on the match-weight distribution (validity
  conditions checked vs silver labels first); **abstention band** (merge / abstain→review / reject)
  sized to the ≤5 h/week budget with **degrade-conservative** tightening; **local-LLM boundary
  pre-annotation** with Qwen3-4B (validated on a held-out sample of the actual band first, never gold).
- **Acceptance:** cross-batch dedup closed for the fuzzy long tail; the threshold reported as an
  estimated FDR under checked assumptions; queue inflow fits the budget or the band auto-tightens.
  **Property/metamorphic tests mandatory.** Threshold promotion stays **human-signed**. **[deps: Gate 2
  (cleaner) or standalone; needs the real-seed run — human] [ADR: 0016/0043/0079/0080/0085]**

### Gate 5 — Tagging *(ADR 0096)*
- **Scope:** tag predicates as statements; controlled taxonomy namespaces (STIX `labels`, `tlp:`,
  `admiralty:`, MISP-style) + free tags; auto-tags from scorers/rules carrying confidence
  (**person-affecting auto-tags routed to the review queue**); a rebuildable **tag index** for faceted
  tag-search; tag queries in the pipe-DSL + Cypher + read API/MCP (so Hermes can filter by tag —
  read only); non-destructive rename/merge/deprecate; UI = tag chips (source-on-hover + confidence
  dot), faceted tag search, **tag→overlay**, bulk-tag in the workbench, tag-driven watchlist alerts.
- **Acceptance:** an analyst can tag entities/claims/cases, retrieve by tag facet fast, and promote a
  tag query to an overlay; auto-tags render as leads with confidence; a person-affecting auto-tag is
  sign-off-gated; every tag carries provenance. **Property test: tagging-of-a-person is
  sign-off-gated + provenance-complete.** **[deps: Gate 3 (statement substrate) + Gate 1 (sign-off UI)]
  [ADR: 0096]**

### Gate 6 — Entity dossier + graph explorer *(docs/70 §4B)*
- **Scope:** the schema→widget registry (extends the ADR-0069 form renderer to entity views + legends);
  the dossier (canonical-anchor chips, schema-generated property sheet, claim timeline, contradictions,
  ranked leads); the Cytoscape.js graph over the hop-capped API with provenance styling +
  `possibly_same_as` → Send to Review; tag chips present throughout (Gate 5).
- **Acceptance:** any entity type (incl. a `wm:` type) renders its dossier from schema with source
  chips + contradictions; the graph explores hop-capped neighborhoods with provenance styling.
  **[deps: Gate 5 for tag chips (soft); Gate 3 for claim timeline] [ADR: docs/70]**

### Gate 7 — Overlay primitive + Monitor map *(docs/70 §2, §4A)*
- **Scope:** the shared `Selection` core + per-surface render adapters (Map/Graph/Widget/Alert);
  MapLibre GL + **self-hosted PMTiles/Protomaps basemap on MinIO** (no external tiles) + deck.gl
  overlays; the overlay stack (toggle/opacity/legend/provenance/confidence); the global **bitemporal
  time scrubber** + diff mode; provenance-first feature popover.
- **Acceptance:** overlays toggle on a self-hosted map; the scrubber drives a shared time cursor; diff
  mode paints additions/retractions; a tag query and a saved query both promote to an overlay.
  **[deps: Gate 3 (bitemporal log), Gate 5 (tag overlays)] [ADR: docs/70 §2]**

### Gate 8 — Marketplace + Tier-0 UI contributions *(docs/70 §7)*
- **Scope:** manifest additions (`listing` / `connect` / `contributes`); the Store UI over the existing
  `config.schema.json` form renderer; **Tier-0 declarative** overlay/widget/panel contributions (host
  renders; zero plugin JS); the install→connect flow with masked secrets + a `test_connection`. (Tier 1
  sanitised-HTMX and Tier 2 gated-iframe are later, if a community pack needs them.)
- **Acceptance:** the Integrations page becomes the Store; a plugin declaring an overlay/widget in its
  manifest lights it up with no frontend edit. **[deps: Gate 7 for overlay contributions] [ADR: docs/70
  §7, 0069]**

### Gate 9 — Dashboards + Query workbench *(docs/70 §4E, §8)*
- **Scope:** gridstack.js dashboard builder (widget = overlay + renderer; `save()/load()` to Postgres;
  **export/import JSON** so packs can ship starter dashboards); CodeMirror 6 query workbench with
  **native Cypher first** (neo4j/cypher-editor, admin-gated) + saved parameterised templates;
  Promote-to-overlay + Save-as-alert. **Decide the pipe layer here** (see §Relegated micro-decisions D3).
- **Acceptance:** an operator builds and shares a dashboard; runs Cypher + templates with autocomplete;
  promotes a result to an overlay/alert. **[deps: Gate 7] [ADR: docs/70 §8]**

### Gate 10 — Ask console *(docs/70 §4E; S5)*
- **Scope:** the server-rendered, side-loadable Hermes chat panel (dockable on any surface + a standalone
  page), streaming through Hermes over the read-only MCP + the LiteLLM gateway. Read/investigation only
  (write tools = Phase 6, gated — D6).
- **Acceptance:** the operator opens an authenticated in-app chat, asks a graph question, streams an
  agent answer, and can jump from an answer to an entity/overlay. **[deps: S1–S3 Hermes stack (shipped);
  Gate 6/7 to link into] [ADR: 0089/0090/0091/0092/0093]**

### Then — domain packs (each a pack, not a gate-heavy build)
CTI enrichers (persona, Phase-4 first: passive-DNS / cert-transparency / JARM-JA3 / STIX from
OpenCTI-MISP) → **conflict-reporting pack second** (ACLED/CAMEO `wm:Event`, geo+time native — shows off
the map/overlay surface) → markets / pandemic. Each = schema + manifests + mappers + default overlays +
optional dashboard; `wm:` naming decided at that pack's spec.

---

## 2. Operator runbook (the human-only tasks)

These are the tasks the operator (Mithat) performs; the fleet cannot. Do them when the referencing gate
needs them.

**R1 — Deploy Hermes + MCP (unblocks Gate 10 + the calibration run).** This is the standing Phase-3 S4
RESUME condition. Bring up the `agent` compose profile on the always-on host, then report back:
- the Hermes run command + config dir + the MCP transport bearer key you set (`WM_MCP_TOKEN`);
- confirmation Hermes lists **exactly the 4 read tools** (`get_entity`, `get_neighbors`,
  `get_provenance`, `find_paths`);
- an `llm-egress caller=hermes` log line proving traffic routes through the LiteLLM gateway.
- *Where:* `deploy/compose.yaml` (`--profile agent`), `deploy/hermes/config.yaml`,
  `deploy/hermes/README.md`. Secrets are `${VAR}` placeholders in a host `.env` (gitignored).

**R2 — First real-seed calibration run (unblocks Gate 4's calibration).**
- Enable a first real source set from the Store/Integrations page (recommended seed:
  **OpenSanctions** — FtM-native, canonical-ID rich, ideal silver-label substrate; add **ACLED** for
  the conflict pack later). API keys go in the host `.env` or the Integrations config form (encrypted
  at rest — never in git).
- Let the driver collect + resolve on cadence; then run the eval harness
  (`resolution/eval.py` + `gold.py`) to produce B³/CEAFe/over_merge_rate on the real corpus, and the
  sufficiency report. Report the numbers back — they set the abstention band.
- *You are providing:* the host, the source API keys, and the "go" — not code.

**R3 — Local boundary-annotation model (Gate 4).**
- On the 6 GB laptop GPU: `ollama pull qwen3:4b` (Q4_K_M ≈ 2.6 GB; use `qwen3:4b-instruct-q5_K_M` if
  you want more quality and still ~3.3 GB — both stay resident with room for context). Keep it loaded
  (`OLLAMA_KEEP_ALIVE=-1`).
- Point it at the LiteLLM **local** mode; the Gate-4 boundary annotator calls it to *pre-annotate* the
  0.5–0.95 band for your confirmation. **First**, run it over a held-out sample of your actual band and
  report the accuracy — only then does it feed the queue (never as gold).

**R4 — Review cadence (ongoing, ≤5 h/week).** Once Gate 1 ships, spend the budget in the review-queue
UI. Your verdicts are the calibration labels; when the queue would exceed the budget the band
auto-tightens (you don't have to manage that).

---

## 3. Comms & compliance (given public-flip = reports-only)

Because only **generated reports/briefings** go public (not the code), the risk surface shifts from
open-sourcing to **publishing intelligence about real people**:
- Every public report must carry **leads-not-verdicts framing + provenance** (a "how this was derived /
  sources & confidence" footer) — defamation/accuracy exposure lives here, not in a LICENSE.
- The **DPIA + Art-14 transparency** work (review F10) still matters — you are the controller of the
  personal data you process, and publishing about individuals raises the bar. Keep it on the list even
  though there's no commercial offering.
- **Build-in-public "method track"** (the gate fleet / ADR discipline) is still available and low-risk —
  it discloses *approach*, not code or targets. Run it **after Gate 0** (truth-up), on the AI/eng
  audience; keep the product/report track separate and provenance-forward.
- LICENSE / open-core / data-licensing audit are **deferred** (no code goes public) — revisit only if
  the repo itself is ever released.

---

## 4. Relegated build-time micro-decisions (decide in the owning gate, not now)

- **D3 (Gate 9) — the pipe query language:** (a) WM-QL thin custom Sumo-like DSL you'd maintain; (b)
  PRQL (zero-maintenance, less Sumo-like); (c) Cypher + templates only. Cypher ships first regardless.
  *Fable's lean:* (a) if the operator will value the familiarity enough to own a small Lezer grammar,
  else (b). Confirm with the operator at Gate 9.
- **wm: naming + `wm:Place` (per pack):** decided at each pack's spec (ADR-per-namespace).
- **Backfill fidelity per cohort (Gate 2):** re-map raw vs accept witness-level lineage — decide from
  the fidelity spike, record in the ADR.
- **Tier-1/Tier-2 plugin UI (Gate 8+):** only if a community pack needs bespoke interactivity; default
  Tier 0.
- **Abstention-band parameters (Gate 4):** set from the real-seed numbers (R2); person-affecting →
  human-signed.

---

*Answers folded in: A2 (Gate 0 now) · B1 (Gate 2 then Gate 3 per rec) · B2 (FtM pin, Gate 0) · B3 (drop
wm:Article, Gate 0) · B4 (DR rebuild-from-Postgres, Gate 3) · C1 (R2) · C2 (review workflow, ongoing) ·
C3 (Qwen3-4B, R3) · D (defaults stand) · E1 (conflict second) · F1 (retire ledger, Gate 0) · F2
(reversibility check, Gate 0) · F3 (build-in-public after Gate 0) · F4 (reports-only, §3). Tagging =
Gate 5 (ADR 0096).*
