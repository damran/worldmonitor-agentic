# 60 — Total Review Questionnaire

> A single place to resolve every open clarification across the whole engagement — the strategic
> review (`50_FABLE_REVIEW.md`), the operator decisions (ADR 0094), the storage-direction
> confirmation (ADR 0095), and the UI/experience design (`docs/70_UI_AND_EXPERIENCE.md`). Answer
> inline (or tell me and I'll record an ADR / update the docs). Each item states **why it matters**,
> **my recommendation/default**, and **what it blocks**.
>
> **Already settled — not re-asked here** (see ADR 0094 / 0095): commercial intent (none/open-core),
> zero-egress claim dropped, no multi-tenancy, persona = CTI/L3-SOC, no paid products, ≤5 h/week
> review, claude-headless retained, parallel stores (Postgres statement-log SoR + Neo4j projection).
> Those are closed; this doc is only the *residue* and the *downstream* choices.

---

## A. Sequencing & immediate next action

**A1. What do I build/do first?**
- *Why:* everything below is downstream of picking the first move; you have one operator and a fleet.
- *Options:* (i) **F5 truth-up sprint** — 1–2 days, docs match the repo, cheap credibility, unblocks
  nothing technical but everything reputational; (ii) **statement spine G-C** (ADR 0095 step 1 —
  start dual-writing statements); (iii) **review-queue UI** (§4D / review F4.1 — safety + labels);
  (iv) **the operator deploy** (S4 Telegram brief + first real-seed calibration run — the only thing
  blocked on *you*, not the fleet).
- *My recommendation:* **A1 = the operator deploy (iv) in parallel with the F5 truth-up sprint (i).**
  The deploy is the one item only you can do and it unblocks two Musts (calibration + the agent
  layer); the truth-up is cheap and I can run it as a fleet gate immediately. Then G-C, then the
  review-queue UI.
- *Blocks:* the whole roadmap ordering.

**A2. Run the F5 truth-up sprint now, as a fleet gate?** (Regenerate the ADR index [stops at 0035],
flip ~20 merged-but-PROPOSED ADRs, refresh/retire the stale GATE_LEDGER, record shipped Phase-3
slices in the roadmap, purge stale `tenant_id` + zero-egress language, update `docs/20` §2.3 for the
storage change.)
- *My recommendation:* **yes, now** — it's a 1–2 day gate and the doc drift already misled a careful
  reader (the review bundle's own digest). *Blocks:* build-in-public credibility; safe agent sessions.

---

## B. Architecture, data & ontology

**B1. Storage inversion (ADR 0095) — go at the full F1 sequence, or bank steps 1–2 only for now?**
- *Why:* the full sequence is ~3–5 fleet-weeks; steps 1–2 (dual-write statements + backfill) already
  bank the audit substrate with no user-facing change and low reversal cost.
- *My recommendation:* **do steps 1–2 soon** (they compound with everything), **schedule the projector
  cutover (3–5) after the review-queue UI + incremental ER**, so you're never mid-migration without a
  working system. *Blocks:* incremental ER, DR story, value-level erasure, watchlist diffs.

**B2. FtM version pinning policy** — write the missing dependency-policy ADR? (Pin exact FtM version,
vendor the schema YAMLs as data, define an upgrade cadence + a schema-diff gate.)
- *Why:* L2's foundation is currently an unpinned external dependency (review §12.6); cheap to close.
- *My recommendation:* **yes**, fold into the ontology-governance work. *Blocks:* nothing urgent;
  reduces a standing risk.

**B3. `wm:Article` reconciliation** — `docs/20` §4 proposes `wm:Article` but Phase 2 shipped FtM
`Article`. Keep FtM `Article` (drop the `wm:` proposal), or is there a reason for the extension?
- *My recommendation:* **keep FtM `Article`**, delete the `wm:Article` row (fold into F5). *Blocks:*
  ontology-doc accuracy only.

**B4. Neo4j remains Community forever (ADR 0094 D5) — confirm the DR posture is "rebuild-from-Postgres."**
- *Why:* CE has no online backup/HA; once Postgres is SoR (ADR 0095), the *only* DR story is replaying
  the statement log into a fresh Neo4j. This makes the scheduled rebuild-and-diff job load-bearing.
- *My recommendation:* **confirm** — and treat the rebuild-and-diff job as a first-class,
  paged-on-failure operational control, not a nicety. *Blocks:* the DR guarantee.

---

## C. ER, calibration & the safety loop

**C1. The first real-seed calibration run** (review F3.1 / G-B) needs your host + source keys — when?
- *Why:* the ER harness (ADR 0043) is a ruler with no measurement taken; every "calibrate before you
  conclude" claim is promissory until this runs. It's operator-blocked.
- *My recommendation:* **bundle it with the operator deploy (A1)**. *Blocks:* an honest threshold, the
  abstention-band width, all public calibration claims.

**C2. Abstention-band sizing to the ≤5 h/week budget (ADR 0094 D6)** — confirm the mechanism: when the
review queue's steady-state inflow would exceed ~5 h/week, the band **tightens automatically**
(degrade-conservative), rather than debt accruing.
- *Why:* a queue that silently backs up becomes a de-facto auto-approve — the one failure mode that
  quietly breaks the safety model.
- *My recommendation:* **confirm the degrade-conservative coupling as a hard rule.** The band-width
  parameters themselves stay human-signed (person-affecting). *Blocks:* the incremental-ER decision
  logic (review F2/F3.4).

**C3. Local-LLM boundary pre-annotation (review F3.3)** — OK to run a local
DeepSeek-R1-Distill-14B-class model to *pre-annotate* the 0.5–0.95 boundary band for your confirmation
(never as gold directly), after validating it on a held-out sample of *your* band?
- *My recommendation:* **yes** — it's sovereignty-clean (local), and directly fixes the thin-boundary
  weakness; the human still confirms. *Blocks:* calibration throughput at 5 h/week.

---

## D. UI & experience (from `docs/70` §12 — restated for one-place answering)

**D1. Default landing surface:** Desk (workbench — my rec for CTI persona) / Monitor (map-forward,
founding feel) / a saved Dashboard? *(Config-only; sets the default.)*

**D2. Graph explorer primary lib:** Cytoscape.js (hundreds of nodes, richer interaction — my default)
vs sigma.js/Graphology (thousands-plus, WebGL, thinner)? *(Depends on your typical subgraph size.)*

**D3. The pipe query language:** (a) WM-QL — a thin Sumo-like custom DSL you'd maintain (most
familiar); (b) PRQL — zero-maintenance but FROM-first/less-Sumo-like; (c) Cypher + templates only?
*(Cypher ships first regardless; this is the friendly layer on top. Lean (a) if you'll value the
familiarity enough to maintain a small grammar, else (b).)*

**D4. Basemap:** self-hosted Protomaps planet PMTiles (~120 GB on MinIO, fully sovereign — my rec) vs
a lighter regional/coarse basemap? Any style preference (dark / satellite-like / minimal)?

**D5. Plugin UI trust ceiling:** cap third-party contributions at Tier 1 (declarative + sanitised HTML,
no plugin JS ever) or allow Tier 2 (gated sandboxed-iframe islands) for community packs? *(Only
matters if you expect community-authored rich widgets; built-in packs use Tier 0/1.)*

**D6. Chat scope:** Ask console read/investigation-only (current MCP surface) now, with write-tools
(run enrichments / start investigations from chat) anticipated but deferred to Phase 6?
*(My rec: read-only now, design the affordance, gate the capability.)*

**D7. Dashboards/Views sharing:** personal-only, or export/import JSON so a domain pack can *ship* a
starter dashboard? *(My rec: export/import — makes packs feel complete; no multi-tenancy needed.)*

---

## E. Ontology domain packs (the CTI → pandemic → markets → conflict mandate)

**E1. Pack build priority after CTI.** CTI is the persona (built into Phase 4 first per ADR 0094 D4).
Of the other three you named, which next: **conflict reporting** (ACLED/CAMEO events — geo+time
native, best shows off the map/overlay surface), **market investments** (scored leads — best shows off
dashboards/calibration), or **pandemic** (the cleanest test of the "new domain = pack" claim)?
- *My recommendation:* **conflict reporting second** — it reuses ACLED (already referenced in
  `docs/20` §6 as gold event labels) and makes the founding map/overlay surface sing. *Blocks:* the
  Phase-4/5 enricher backlog order.

**E2. `wm:` naming + `wm:Place` decision** (open in `docs/20` §10): resolve `wm:Place` vs extending FtM
`Address`, and lock the per-domain `wm:` entity names, as each pack is specced (ADR-per-extension)?
- *My recommendation:* **decide per-pack, at pack-spec time** (not all up front) — keeps it additive.
  *Blocks:* nothing now; each pack's first gate.

---

## F. Governance & communication

**F1. GATE_LEDGER.md** — keep maintaining it, or retire it in favour of the roadmap-as-ledger? It's
stale past ADR 0085 and self-contradicts on ADR 0080.
- *My recommendation:* **retire it** (the roadmap + ADR headers already carry the state); one less
  thing to drift. *Blocks:* part of the F5 sprint.

**F2. Self-classified reversibility check** (review §4b) — add "verify the ADR's reversibility &
person-affecting tags against the diff" to the checker/judge mandate, and require a human co-sign line
on any ADR that self-tags non-sensitive or waives a human fork?
- *My recommendation:* **yes** — cheap, closes the one real governance gap (the planner writes the ADR
  *and* its own risk tags today). *Blocks:* nothing; hardens the method you'd talk about publicly.

**F3. Build-in-public timing & channel** — after the F5 sprint + privacy notice (my strong rec), and
on which channel/audience (engineering/AI vs OSINT/CTI)? The review's rewritten posts (§4a) are ready
but should run through the claim-vs-reality table once more before anything is published.
- *My recommendation:* **after F5**; lead the *method* track to the AI/eng audience, the *product*
  track (answers-with-receipts) to the CTI audience, cross-linked, never blended.

**F4. When is the "public flip"?** (AGPLv3 lands then, per ADR 0094 D1a; repo is private today so
nothing is licensed yet.) Is a public release on the 12-month horizon, or is this staying private
indefinitely?
- *Why:* it sets whether the LICENSE + data-licensing audit + Art-14 notice are "soon" or "someday".
- *My recommendation:* decide the *intent* even if the date is soft — it changes how much of the
  compliance-as-schema work (review F10) is worth front-loading.

---

## G. Anything I assumed that you'd want to change?

These are calls I made autonomously that are reversible — flag any you'd do differently:

- **Committed the review bundle's stale digest as-authored** (corrections live in `50_*` §0) rather
  than editing the digest — preserving it as historical input. OK, or fold the corrections back in?
- **Recorded ADR 0095 as reversing locked decision #2 and edited CLAUDE.md/AGENTS.md/.clinerules**
  ground truth to the parallel-store direction (marked "in transition"). Confirm the ground-truth
  wording reads right to you.
- **`docs/70` is workbench-first with Monitor as a first-class mode** (not map-first), on the CTI
  persona + review evidence. If the founding "world monitor" feel matters more to you than the CTI
  daily loop, say so and I'll flip the default (D1) — the architecture supports either.
- **Overlay = Lens with a shared `Selection` core + per-surface render adapters** (not one fat
  object). This is the load-bearing UI abstraction; confirm the model makes sense to you.

---

*Answer any subset; unanswered items keep my stated defaults. I'll turn your answers into ADR
updates, a `docs/70` v0.2, and the next build gate.*
