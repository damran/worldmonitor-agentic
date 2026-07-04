# 50 — Strategic Architecture & Product Review

> **Reviewer:** Fable 5, acting as principal systems architect and product strategist.
> **Date:** 2026-07-04. **Scope:** per `40_REVIEW_CHARTER.md` — architecture, decisions, technology
> choices, product strategy, communication. Explicitly *not* code review; correctness is the gate
> fleet's job.
>
> **Method & evidence.** This review is grounded in: (a) the bundle (docs 00–40); (b) primary reads
> of the repo (`docs/decisions/` corpus, `docs/00–60`, `settings.py`, package shape/LOC, compose
> topology); (c) a mid-2026 technology and market landscape sweep (graph substrates, ER, durability,
> agent/LLM, comparables/legal) in which every load-bearing factual claim was adversarially
> fact-checked against primary sources (30 checked: 25 confirmed, 5 modified in ways that don't
> flip a recommendation, 0 refuted — corrections incorporated); and (d) **three clean-slate designs
> produced by architect agents with deliberately different priors** (provenance-native,
> infrastructure-first, product-led), used as a convergence test for Track 2. Honesty note on (d):
> the architects read the same bundle — whose charter itself seeds the bitemporal fork — and share
> a model family, so where they agree I treat it as a **robustness check on the reasoning, not
> independent evidence**; the load-bearing arguments below rest on external evidence (ecosystem
> production practice, verified primary sources), not on the panel's vote.

---

## 0. Corrections to the review bundle itself (read first)

Two premises in `10_SYSTEM_DIGEST.md` are wrong against the repo, and one of them matters a great deal:

1. **The merge guard is NOT in alert mode.** The digest (§4, §12.2) and the charter (Track 1 dim 2)
   state the guard "defaults to alert… flipping to block is required before production and has not
   happened." In fact **ADR 0031** (accepted 2026-06-21) flipped the default and fulfilled ADR 0024's
   obligation, and the code confirms it: `src/worldmonitor/settings.py:83` —
   `merge_guard_mode: Literal["alert", "block"] = "block"`. GATE_LEDGER records the return-to-block
   and fail-closed M-3 items as CLOSED. Your platform's single most important safety claim — *"a
   sensitive merge is blocked and queued for human sign-off, not just logged"* — is **already true**,
   and your own review bundle understates it. I treat this both as a correction (Track 1 findings
   below assume block mode) and as a Track 3 finding: your documentation estate has drifted far
   enough that it now misinforms *you*, in the conservative direction this time, but drift has no
   preferred direction.
2. **"93 recorded decisions with explicit reversibility classification" (digest §10) is overstated.**
   Reversibility/revisit-trigger discipline is systematic only from ADR 0054 onward (consistent
   0056+); decisions 1–15 and — with isolated exceptions such as 0019's reversibility/upgrade-trigger
   section and 0037 — 0016–0036 and most of 0038–0053 carry no classification. Roughly half the
   corpus is classified. Still genuinely distinctive — but say "since mid-June 2026" when you say it
   publicly.
3. Minor: the roadmap's Phase-6 plan "graph explorer: **Neo4j Bloom first**" almost certainly doesn't
   work on self-hosted Community Edition (Bloom is an Aura/commercial feature — verify before
   planning around it).

---

## 1. Executive summary

WorldMonitor's thesis is right, its safety spine is real (and better than its own digest claims),
its build discipline is genuinely unusual, and its ecosystem bets (FtM, canonical anchors, central
ER, MCP-as-seam) are the correct ones. The deep problem is **one storage-model decision made at the
foundation**: the *resolved, merged* graph is the system of record, so provenance, reversibility,
erasure, DR, and streaming resolution all fight the substrate — every tension in digest §12 traces
back to it. The relaxed constraints make this the moment to fix it, because the fix is also the
cheapest path to everything else you want.

**The eight calls, ranked:**

1. **Invert the storage model (Must, F1).** Make a nomenklatura-style **statement + judgement log
   in Postgres the system of record**; demote Neo4j to a **derived, disposable, rebuildable
   projection**. This is how both flagship products of your own ecosystem — OpenSanctions and
   Aleph — already work in production, and ADR 0045 already builds `StatementEntity` fusion
   transiently at merge time: you are one persistence decision away. It structurally dissolves
   merged-node provenance collapse, the dual-write problem, hours-scale DR, irreversible merges,
   value-level erasure, and every Neo4j-Community ceiling at once.
2. **Make ER incremental and calibrate to an error-rate claim (Must, F2–F3).** Batch-only
   resolution makes "de-dupe before you count" false *as a standing condition* exactly for the
   non-anchored fuzzy long tail (anchored entities already converge via durable IDs). Compose
   Splink's documented incremental primitive with the periodic full re-cluster MoJ validates in
   production; replace the bare 0.92 with an FDR-denominated threshold plus an **abstention band**,
   with local-LLM pre-annotation of boundary pairs for the human queue.
3. **Build the review-queue UI and watchlist/diff surface now (Must, F4).** The review queue is
   simultaneously the safety control, the calibration label factory, and the demo — and it has no
   hard dependency on (1), so it starts in parallel. The consumable unit is not the node; it is the
   **answer with receipts**. Order: review queue → watchlists/diffs → dossier → explorer last.
4. **Truth-up before you build in public — in both directions (Must, cheap, F5).** The docs
   understate shipped safety (guard mode, §0) and overstate elsewhere (merged-node provenance,
   standing dedup, classification coverage). ADR index stops at 0035; ~20 merged ADRs still say
   PROPOSED; GATE_LEDGER stale and self-contradictory; the roadmap doesn't record the shipped
   Phase-3 slices; stale `tenant_id` language survives. A 1–2 day sprint buys credibility you
   cannot buy later.
5. **Reposition sovereignty from identity to policy; frontier extraction in the ingest path
   (Should → Must for Phase 4, F6).** The sensitive assets are analyst intent and the aggregate
   graph, not public newswire text. At plausible volumes, frontier extraction is
   order-$10²–10³/month (unit economics in F6) and measurably better than local models on
   multilingual relation extraction; keep a local-mandatory lane for sensitive workloads. **Retire
   the claude-headless mode now** — pull the exit trigger ADR 0091 already recorded, before any
   public post.
6. **Keep Hermes at the edge; never route self-improvement through it (Should, F8).** Pin +
   hash-verify (the March 2026 LiteLLM supply-chain compromise is the precedent), review its
   skills/memory files as an injection-persistence surface, and mechanise §4c as **config-change
   PRs through the gate fleet you already have**. Keep §4b (fine-tuning) deferred indefinitely.
7. **Don't rebuild multi-tenancy; do shape new tables for it (Should, F11-T).** Single-tenant per
   deployment is the right product and your best GDPR position; a `scope` column on the new
   statement tables costs one column now vs ADR-0042-in-reverse later (quantified in F11-T).
8. **Compliance as schema before any commercial step (Must-before-commercial, F9–F10).** LIA per
   source as a required manifest field; a DPIA now; an Art-14 transparency plan; retention TTLs; a
   query-audit log; **a LICENSE file** (none = all rights reserved); OS-Pairs (CC BY-NC) fenced
   from any commercial artifact.

**If I could change only three things:** the storage inversion (1); incremental ER + honest
calibration (2); and the review-queue/watchlist surface (3). Together they make the two maxims true
as standing conditions, make the safety story enforceable by construction, and give the product its
first demonstrable answers. Almost everything else in this review is either downstream of these or
cheap hygiene.

---

## 2. Track 1 — Improve in place

Findings are ordered by priority (Must → Should → Could); conditional priorities (e.g. F10
"Must-before-commercial") are placed by their *start-now* component. Each finding gives **what /
why / cost / unlocks / priority**. Charter-dimension coverage: F1 (dims 1, 5), F2–F3 (dim 2), F4
(dim 6), F5 (Track-3b overlap; feeds dim 8), F6 (dims 3, 7), F7 (dims 3, 4), F8 (dims 3, 7),
F9–F10 (dim 9), F11 (dim 3, incl. tenancy F11-T), §2.13 (dim 8).

### F1 (MUST) — Statements become the system of record; the graph becomes a projection

**What.** Introduce a durable **statement table** (per-claim rows: subject, schema, prop, value,
dataset/source, `retrieved_at`, `first_seen/last_seen`, reliability, raw pointer, method) and a
**decision/judgement table** (merge/split/negative, evidence, decided-by human-or-model@version,
supersedable) in Postgres as the source of truth. Rebuild the Neo4j graph as a **derived,
idempotent, disposable projection** fed by a transactional outbox → projector, with a versioned
"replay everything" job that doubles as migration tool and DR story. Keep FtM as the vocabulary and
validation contract (C2 untouched); keep the L2 rule — the contract *deepens* from "producers emit
entities-with-provenance" to "producers emit **claims**-with-provenance; the resolved graph is a
view over claims plus decisions."

**Why.** Steel-man first: "Neo4j = sole system of record, no parallel datastore" (locked decision
#2) was the right v0 call — one store, one truth, no sync problem, and the FtM→Neo4j tooling made it
the fastest path to a working spine. And the *intent* of the no-parallel-datastore rule — never two
divergent truths — is correct and should survive. But the implementation now violates that intent:
the graph-queryable projection is admittedly thinner than the Postgres audit tables behind it
(digest §3) — you already have two truths, with the *weaker* one canonical. Every §12 tension traces
to storing the *merged output* rather than the *evidence*:

- **Provenance is thinnest exactly on merged nodes** (§12.5) because `merge_context` can't union
  nested structure — a substrate limitation, worked around with witness maps (ADR 0018/0045). In a
  statement store, a merged entity *is* its N statements; the problem cannot exist.
- **Merges are destructive**, so the catastrophic-merge guard must be perfect *before* the write. In
  a statement store, merge = a judgement row repointing `canonical_id`; un-merge = a superseding
  judgement + reprojection. The guard stops being the last line of defence; belief revision becomes
  the model (this is also the honest answer to the charter's Track-2 "catastrophic-merge-as-belief-
  revision" question — you can have it in Track 1).
- **Dual-write without a saga** (§12.4) disappears: Postgres is the sole write point; the projection
  is async and rebuildable, so at-least-once + idempotent MERGE suffices.
- **DR/HA** (§12.4, ADR 0050's caveats): backup becomes `pg_dump`/PITR of statements; Neo4j
  Community's lack of hot backup, RBAC, clustering, and multi-DB becomes irrelevant, because the
  graph is disposable state. (Verified: CE backup is offline-only `neo4j-admin dump`; online backup
  and clustering are Enterprise-only. GDS Community is also capped at 4 cores — relevant to Phase 5.)
- **Value-level erasure** (WM-ERASE-T2, ADR 0049) becomes `DELETE ... WHERE` + reprojection instead
  of a deferred allowlist-gated write pass.
- **Tier-2 provenance** (ADR 0045, deferred): you stop needing reified `(:Statement)` nodes at all
  except as an optional read optimisation — statements are first-class *where they live*.

The evidence that this is the ecosystem-native move, not an exotic one: **nomenklatura — already
your dependency — is statement-based internally** (statement rows + a Resolver judgement DAG;
entities assembled by grouping on `canonical_id`; merge/unmerge = repointing, sources never
mutated). **Aleph/OpenAleph runs Postgres as system of record with Elasticsearch as a rebuildable
index.** Your merge path already constructs `StatementEntity` per cluster member and fuses
statements (ADR 0045 §Decision-1) — then throws the statements away and persists only the projection.
ADR 0045's rejected alternative (D) — "a Postgres `:Statement` mirror… a parallel model" — was
rejected under a rule whose intent this design *fulfils better than the status quo*: one truth
(statements), N derived views. (The Track-2 architect panel also made this its unanimous #1
divergence — but per the method note, that is a robustness check, not independent evidence; the
evidence is the ecosystem's production practice and your own ADR trail.)

**Cost.** The biggest Track-1 item, but it decomposes into additive, gate-sized steps with low
reversal cost until the final cutover: (1) create tables + start dual-writing statements at merge
time (the fused `StatementEntity` already exists in memory — persist it); (2) backfill; (3) outbox
+ projector; (4) cut the graph writer over to the projector; (5) retire the direct write path.
Roughly 3–5 gate-fleet weeks calendar **as a floor** — two risks must be priced in, not discovered:
(a) **Fold/projection semantics are the hard part** (the same risk §3.11 flags for the clean
slate): deterministic materialisation under decision supersession and re-canonicalisation is where
this design can rot into a mutated projection. So step (3)'s scope *includes* a fold-determinism
property suite and a scheduled full-rebuild-and-diff job — they are the gate's primary invariant
tests, not later hardening; budget contingency accordingly. (b) **Backfill fidelity varies by
cohort:** single-source nodes reconstruct exactly from `prov_*` + audit tables; merged nodes
reconstruct per-property witnesses from `prov_witnesses` (values × witness sets, not original
per-source values); full fidelity needs re-mapping raw landing objects + replaying the judgement
log — and pre-ADR-0045 merges may be lossy. Run a backfill-fidelity spike first and decide
per-cohort (re-map vs accept witness-level lineage), recording the choice in the ADR. Until step
(4) nothing user-facing changes; you can stop after (2) and still have gained the audit substrate.
Requires an ADR superseding locked decision #2's *mechanism* (keep its intent: "one truth, no
divergent stores" — now enforced by construction).

**Unlocks.** F2 (incremental ER wants a persistent resolved index and a durable judgement log —
this *is* that), F4 (watchlist diffs are an indexed scan over `first_seen`/assertion time), F7 (HA =
managed Postgres + rebuildable projection), F10 (lawful-basis/retention as statement columns),
multi-workspace later (one `scope` column now — see F11), per-claim read APIs (`/entities/{id}/claims`),
and the honest version of the provenance claim for Track 3.

**Priority: Must.** This is the "what would you change first" answer to charter dim 1. The L0–L9
layering itself and the L2-is-the-contract rule are **right and battle-tested — keep them**; the
change is *within* L2→L4's realisation, not to the layer model. A labelled property graph is the
wrong *system of record* for an ontology-first, provenance-first product — the flat-`prov_*`
friction was indeed the substrate fighting the goal — but it is exactly the right *projection*.

### F2 (MUST) — Incremental ER: close the cross-batch gap with a hybrid micro-batch cadence

**What.** Keep Splink + nomenklatura (the engine choice survives scrutiny — see F11 for the Senzing
comparison). Change the cadence: (a) persist the resolved corpus as a linkable index (on the F1
statement spine, or standalone if sequenced first); (b) score each arrival window with
`find_matches_to_new_records` against that index **plus** new-vs-new within the window; (c) append
judgements to the durable resolver log (your `resolver_judgement`/sign-off tables are already the
right shape — nomenklatura's key lesson is that *the judgement DAG, not the clusters, is the ER
state*); (d) keep periodic **full re-cluster as the corrective ground truth and drift audit** —
Splink has no incremental cluster maintenance (a new edge can bridge two clusters), so the periodic
re-run is what catches transitive effects. (e) Wire the dormant **pgvector** as ANN candidate
blocking over multilingual name embeddings — recall-oriented dense blocking feeding precision-
oriented Fellegi–Sunter scoring is the standard hybrid, and it directly attacks the cross-script
cases your abjad work (ADR 0073) cares about.

**Why.** ADR 0019 already calls incremental ER "the real answer" but keys the upgrade trigger to
*throughput* ("resolve falls behind ingest"). The sharper fact: the gap is a **correctness** hole,
not a capacity one. Entities carrying canonical anchors converge across batches anyway (durable
anchor-preferred IDs, ADR 0044/0048, unify at the graph MERGE). What never gets compared across
batches is everything *without* an anchor — the news-mentioned person, the transliterated org — the
precise population probabilistic ER exists for. A slow trickle never co-occurs in a window, so C4 is
false as a standing condition at *any* volume. Steel-man of 0019/0026: batch-first was the correct
lowest-regret call to ship a proven resolver, and the bounded-window machinery (quarantine,
dead-letter, guard integration) all carries over. Provenance of the pattern, stated precisely so
the pedigree isn't inflated: `find_matches_to_new_records` is a **documented Splink primitive**
(with the documented limits above — no new-vs-new matching, no incremental cluster maintenance);
MoJ's actual production cadence is **weekly full-batch with no incremental clustering**, which
validates keeping the periodic corrective re-cluster; the hybrid combining the two is **this
review's composition**, not a copied production deployment. The recommendation is a hybrid, not a
rewrite.

**Cost.** ~2 gate-fleet weeks after F1 steps 1–2 (cleaner) or ~3 standalone. Touches the merge audit
trail and referent rewriting — the interaction ADR 0019 warned about — so it is a full-fleet gate
with property tests. Reversal: moderate (falls back to periodic re-batch; the judgement log is
append-only either way).

**Unlocks.** C4 true as a standing condition (the headline claim becomes honest); stream sources
stop accumulating latent duplicates; the review queue receives candidates continuously instead of in
bursts (smooths the solo reviewer's load — see F4).

**Priority: Must** — with F1, the pair that makes the two maxims true.

### F3 (MUST) — Calibration: from expert constant to error-rate claim

**What.** Four moves, in order:
1. **Run the harness on a real corpus** (the blocked slice-1 real-seed run + the sufficiency
   report). This is operator-blocked, not build-blocked — it should be the first thing the deploy
   unblocks, alongside S4.
2. **Mixture-model FDR calibration on the match-weight distribution** (Belin–Rubin style): fit a
   two-component mixture and translate the threshold into "0.92 ⇒ estimated false-discovery rate
   X%" — label-free, and it converts an expert constant into an error-rate claim. State the
   validity conditions rather than assuming them: the method needs reasonably separated components,
   score distributions the assumed shapes can fit, and it is conditioned on your blocking (the
   candidate-pair distribution) — conditions that are weakest exactly in the fuzzy non-anchored
   tail F2 targets. So: sanity-check the fitted mixture against the silver labels and the benchmark
   floor **before** any FDR number is cited, even internally, and treat the output as *a defensible
   internal estimate under checked assumptions*, not a publishable statistic. (Also: EM-estimated
   absolute probabilities are known-miscalibrated; the mixture-fit *threshold* is the defensible
   object, not the raw scores.)
3. **Local-LLM boundary pre-annotation.** The Feb 2026 OS-Pairs paper reports RegressionV1 at 91.3%
   F1 vs GPT-4o 99.0% — and **DeepSeek-R1-Distill-Qwen-14B at 98.2%**, i.e. near-frontier boundary
   adjudication is available *inside your sovereignty perimeter*. Use it to pre-annotate the sparse
   0.5–0.95 band for human confirmation (never as gold directly) — after first measuring the
   model's accuracy on a held-out sample of **your actual boundary band**: OS-Pairs is
   compliance-style data, the 98.2% does not automatically transfer to news-mention entities, and
   the known failure modes (transliteration variance, near-miss identifiers) are over-represented
   in exactly your band. This repairs the thin-boundary weakness of the silver-label plan
   (ADR 0079/0085) without violating the no-manual-adjudication decision's spirit — the human
   confirms, the model triages.
4. **Replace the point threshold with an abstention band** (merge / abstain→review / reject), width
   set by an over-merge risk budget. With block mode already the default, this is the natural next
   step: the guard becomes one input into a three-way decision rather than a binary gate after a
   binary threshold.

Also: decide the **OS-Pairs licensing posture** now (CC BY-NC — confirmed, including that the arXiv
paper's CC-BY badge covers the paper text only). If any commercial path is live within 12 months,
either get the commercial data license or fence OS-Pairs into a non-shipping eval enclave.

**Why.** The harness (ADR 0043) is a ruler with no measurement taken; every downstream claim
("calibrate before you conclude") is promissory until slice-1 runs. G7 promotion remains
human-signed (person-affecting — correct, keep).

**Cost.** Steps 1–2: days. Step 3: ~1 week incl. spot-check protocol. Step 4: ~1 week, person-
affecting → human sign-off on the band parameters. **Unlocks:** the honest public claim; threshold
changes become evaluable (prerequisite for §4c ever touching ER); reviewer time redirected to
exactly the pairs that move calibration.

### F4 (MUST) — Consumption surface v1: the review queue is the product's front door

**What.** Build, in this order: (1) a **review-queue web UI** (the sign-off CLI, promoted: side-by-
side entity cards, statement-level evidence diff, one-keystroke approve/reject — every verdict lands
in the gold-judgement table); (2) **watchlists + diff alerts** ("what changed about X since t" — an
indexed scan once F1 exists; delivered via the existing TelegramNotifier and S4 briefings); (3) an
**entity dossier page** (claim timeline, per-claim source/reliability, *contradictions surfaced* —
competing values for one property, trivial in a statement store); (4) a graph explorer **last**, and
not Bloom (see §0.3) — a bounded Cytoscape.js/sigma.js view over the existing hop-capped API is a
weekend-scale slice when it's actually needed.

**Why.** The charter asks for the "minimum analyst experience that makes value demonstrable." All
three Track-2 architects, from different priors, inverted the current build order the same way: the
review UI first (it is the safety control *and* the label factory *and* the thing that makes the
solo-reviewer model sustainable), answer surfaces next, explorer last. Nobody buys a graph; they buy
**answers with receipts** — and your provenance discipline means every rendered sentence can carry a
citation, which is the differentiator versus every feed tool. This also resolves the charter's
"should the analyst surface drive the API rather than trail it" — yes: the API's nouns should become
analyst nouns (`review_items`, `watchlists`, `alerts`, `claims`, `dossier`) rather than only graph
nouns (`entities`, `neighbors`, `paths`).

**Cost.** Review UI: ~1–2 gate weeks (HTMX/Jinja2 like the Integrations page — no SPA). Watchlists/
diffs: ~1–2 weeks *after* F1 (cheap diffs need assertion-time). Dossier: ~1 week. **Unlocks:**
demonstrable value for Track 3's build-in-public moment; calibration labels as an operating
byproduct; the S5 operator console gets something to link to. **Priority: Must** (the review UI at
minimum), because it compounds: every week without it, reviewer effort produces zero labels.

### F5 (MUST, cheap) — The truth-up sprint: make the record match the repo

**What.** One 1–2 day hygiene gate: regenerate the ADR index (0036–0093 are unindexed — automate
index generation from headers); flip the ~20 merged-but-PROPOSED ADR statuses (0040–0061 cluster,
0086/0087/0089/0090); back-annotate 0024 ("fulfilled by 0031") and fix 0031's two broken relative
links; refresh or formally deprecate `GATE_LEDGER.md` in favour of the roadmap-as-ledger (it
contradicts itself about ADR 0080 and is stale past 0085); record the shipped Phase-3 build slices
S1–S3b (ADRs 0089–0093 / PRs #149–#153) under the roadmap's Phase 3 and refresh the stale
"Stage-4 ★ CURRENT" header — while leaving Phase 3's four *operational* acceptance checkboxes
honestly unticked until the operator deploy (G-B) lands, since ticking them now would manufacture
exactly the drift this sprint exists to remove; purge
stale `tenant_id`/"SaaS-grade tenants" language from `docs/20_ONTOLOGY.md` §2, `docs/40_ROADMAP.md`
Phase 0, and `docs/decisions/README.md` rows 14/31; correct `docs/fable-review/10_SYSTEM_DIGEST.md`
(guard mode; reversibility coverage); mark `ARCHITECTURE_REVIEW.md` as a dated snapshot with a
banner (it presents fixed items as open). Then add a cheap standing control: a docs-drift checklist
item in the fleet's park/merge step ("roadmap ticked? ledger row? index regenerated? status
flipped?") — the byte-identical CLAUDE.md mirror test proves you know how to enforce hygiene when
you decide it matters.

**Why.** Three reasons in ascending order: (a) a newcomer (or a future agent session — the digest
error proves it) reconstructs wrong state from the corpus; (b) reversibility-classification and ADR
discipline are your **communicable differentiator** (Track 3), and the first sophisticated reader
who checks will find the index stops at 0035; (c) *your own agents read these documents as ground
truth* — doc drift in an agent-built system is not cosmetic, it is a defect in the build system
itself. **Cost:** 1–2 days. **Unlocks:** credibility for build-in-public; safer agent sessions.

### F6 (SHOULD now, MUST for Phase 4) — LLM posture: sovereignty as policy; extraction in the ingest path

**What.** Two coupled changes:
1. **Reposition the three-mode selector** from "local by identity" to **per-workload routing
   policy**: frontier models (via ZDR/enterprise agreements, still through the single LiteLLM
   choke point with `llm-egress` audit) become the *default lane for public-source text* —
   extraction, summarisation, briefing synthesis; **local stays mandatory** for analyst
   queries/watchlist context, person-affecting adjudication (the F3 boundary annotator), and an
   air-gapped deployment profile. **Retire the claude-headless mode** (ADR 0091 mode 2). Credit
   where due: the ADR already documents the ToS/brittleness caveat, ships the mode off-by-default,
   and records the exact replacement path (a first-party API mode) as its revisit trigger — this is
   not an unmanaged landmine. The recommendation is narrower: *pull that recorded exit trigger
   early*, on your timeline rather than Anthropic's, because the build-in-public moment converts a
   documented internal caveat into a public attack surface. Do it before any posts.
2. **Build the LLM→FtM extraction mapper** as a plugin in the *ingest* path (Phase 4's first
   slice): article text → FtM statements with `method=llm@model+prompt_hash`, char-span pointers
   into the raw landing object, reliability capped at a low tier, and one hard rule — **machine-
   extracted claims corroborate but never anchor**: never the sole basis for a merge, a sensitivity
   classification, or any person-affecting output. Prompt-injection containment falls out of the
   shape: extractor output is schema-validated statements, never tool calls.

**Why.** The honest sovereignty threat model (charter Track 2 asks for it; it applies to Track 1):
the assets are (a) analyst intent/queries, (b) the aggregate graph, (c) subject personal data in
context. Sending a *public news article* to a frontier API under a zero-data-retention agreement
leaks essentially nothing an adversary couldn't read on the newswire; what must never leave by
default are queries and graph context — which is exactly the local-mandatory lane. Capability gap
is real and measured: local ~5–10 F1 points behind frontier on schema-constrained news extraction,
10–20+ on multilingual relation extraction (non-OSINT benchmarks — validate on your golden set per
ADR 0043 before trusting any number). Cost is not the objection it was — but price it per article,
not per slogan, because your real feed volume is unmeasured (the source inventory catalogues ~531
news feeds; measure throughput before committing): assume ~2k input tokens/article including
prompt overhead + ~500 output. At Haiku-4.5 batch rates ($0.50/$2.50 per MTok) that is ≈ **$0.23
per 100 articles**, i.e. ≈ $2.30/day at 1k articles/day, ≈ $23/day (~$700/mo) at 10k/day, ≈
$230/day at 100k/day; Flash-Lite-class models cost roughly 3–4× less, Sonnet-class ~5× more.
Order-of-magnitude conclusion: at any plausible near-term volume the extraction bill is
$10²–low-$10³/month — an economics argument for hybrid routing, not a blocker. Fine print that
survives fact-check: ZDR is contractual, not cryptographic; query metadata still leaks; and
Anthropic's newest top tier (Fable 5) requires 30-day retention — ZDR eligibility sits at Opus
tier. Route accordingly. **Cost:** selector reposition = config + ADR (days); extraction mapper ≈
2 gate weeks.
**Unlocks:** Phase 4's actual value (the unstructured long tail is where cross-source relationship
value lives — without it the thesis covers only structured registries); the fusion layer stops
being decorative. **Flag:** this is the one recommendation that changes a *product identity*
statement — see Open Question 2; my recommendation is per-workload with a marketed sovereign
profile, but it is the operator's call.

### F7 (SHOULD) — One-node ops backbone: boring Postgres-native durability; no Temporal yet

**What.** (1) Replace the `threading.Lock` single-flight with a **Postgres advisory-lock lease +
fencing-token row** (heartbeat-renewed; token checked on writes) — the boring, proven pattern, and
the prerequisite for ever running two driver replicas. (2) The **outbox + idempotent projector**
from F1 (they're the same work item). (3) Adopt **DBOS Transact** (MIT, library-not-server, durable
workflows checkpointed in the Postgres you already run; v2.26.0 released 2026-06-30) *incrementally*
for connector/resolve workflows that need retry/resume — it composes with the asyncio driver rather
than replacing it. (4) Skip, for now: Temporal (4 services + visibility store + upgrade lifecycle —
a second product to operate; Temporal Cloud's $100/mo floor is the honest entry *when multi-node
arrives*), Kafka/Redpanda (a second durability domain with no capability gain at your volumes —
Postgres-CDC/outbox covers it; Debezium Server or logical-replication consumers if a tail is ever
needed), and Prefect/Dagster (cadence + lineage UI, not durability — revisit only if backfill pain
is real, as Dagster OSS *calling* your task layer).

**Why.** The digest's §12.4 self-diagnosis is right; the fix under one operator is to make the
single node *correct* and every scale-out step *config rather than re-architecture*. The migration
order once cloud is wanted: **managed Postgres first** (after F1 it is the sole source of truth —
PITR = the whole DR story), graph second (stay Community + rebuild-as-RTO, or Aura — noting
verified fine print: base AuraDB excludes GDS; Professional can enable a shared-resource Graph
Analytics plugin; Business Critical needs separate pay-per-session Graph Analytics/AuraDS), workers
third (DBOS queues span replicas on the same Postgres). **Cost:** lease ≈ days; DBOS adoption ≈
incremental; the rest is deferral. **Unlocks:** HA path with no rewrite; deletes the §12.4 ceiling.
**Flag:** the Temporal-from-day-zero answer is correct for Track 2's clean slate (small team,
cloud-assumed); it is *not* the right next step for this repo with one operator — that divergence
between tracks is deliberate.

### F8 (SHOULD) — Agent layer: Hermes at the edge, the loop's risky parts owned; §4c as config-PRs

**What.**
1. **Keep Hermes** for the operator-facing surface (Telegram/chat gateway, cron briefings, session
   memory) — adoption remains defensible: healthy upstream (v0.18.0, 2026-07-01; ~1–2 week release
   cadence; MIT), already isolated as a container speaking only authenticated read-only 4-tool MCP,
   and reversible by construction (ADR 0089's own reversal analysis is correct). But treat it as an
   **untrusted fast-moving appliance**: pin exact versions + hashes and lag releases (the March 2026
   LiteLLM PyPI compromise — backdoored wheels live for ~40 minutes — is the concrete precedent for
   this stack; LiteLLM itself should be pinned the same way); egress-restrict its container; and put
   its **skills/memory files under review cadence** — they are agent-writable persistence that
   survives sessions, i.e. a prompt-injection persistence surface your gating model doesn't cover
   (4a "touches no platform data" is true but not the point — it touches future agent *behaviour*).
2. **Mechanise §4c on infrastructure you already trust: git + CI + the gate fleet.** A proposal is a
   **PR changing a versioned config artifact** (threshold, weight, rule, prompt); CI runs the
   evaluation harness and publishes metric deltas; promotion = merge on green + required checks;
   person-affecting paths (`config/er/*`, scoring) carry CODEOWNERS + mandatory human review;
   rollback = revert. No bespoke promotion machinery, and the audit trail is git history — which
   your ADR/ledger discipline already treats as the system of record for decisions.
   The charter's three named sub-questions, answered by name:
   - **Reward signal:** per parameter class, the metric delta on the class's own held-out
     evaluation — for ER parameters, the ADR-0043 harness (B³/CEAFe/over-merge-rate on the golden
     partition + benchmark floor); for scorers, each scorer plugin's *own* calibration/backtest
     eval, which it must ship as a condition of registration (the same rule as
     property-tests-per-gate). There is deliberately **no generic reward signal** and no generic
     eval framework built in advance — a proposal with no eval substrate is unpromotable by
     definition, which is the correct failure mode.
   - **Safe ranges:** versioned per-parameter bounds living *in the config artifact itself*
     (min/max plus a max per-promotion delta), so a proposal outside bounds cannot even be
     well-formed. The bounds are themselves person-affecting configuration: changing a bound is a
     human-signed change, always. Within bounds + non-sensitive class + eval-green = auto-mergeable;
     everything else escalates.
   - **Scoring a person-affecting proposal:** never by metrics alone — eval-green is *necessary*,
     and a human signature is *unconditionally required* (CODEOWNERS-enforced), with the review
     showing the metric deltas, the affected-entity sample, and the rollback plan. Bounded
     auto-tune (docs/50 §4c's own language) applies only to non-sensitive parameters within
     pre-declared bounds.
3. **Keep §4b deferred indefinitely** and re-key its trigger: not "Phase 6" but "agent runs became
   high-volume and homogeneous with a frozen tool surface" — the 2026 consensus (and e.g. Shopify's
   fine-tuned-agent experience) is that per-deployment trajectory fine-tuning pays only under those
   conditions; skills/context capture the adaptation win at zero training cost.

**Why.** The charter asks whether adopting a general self-improving runtime is the right call versus
a thin loop. Split the question: for the *assistant experience*, adoption is cheap and reversible
behind MCP (keep). For the *self-improvement differentiator*, routing it through third-party
machinery you neither authored nor audit would be backwards — and you don't need to: your gate
fleet **is** a propose→evaluate→gate→promote engine with a human boundary; §4c is a special case of
what you already do to your own codebase, with configs instead of code. All three Track-2 architect
agents landed on "thin loop / mechanise gating on existing rails" (a robustness signal, per the
method note); the landscape research
lands on "keep Hermes at the edge, swap later if S4/S5 shows you use 10% of it" — these are
compatible, and the MCP seam is what makes the fallback (a ~2-week PydanticAI/Claude-Agent-SDK thin
loop) a swap rather than a rewrite. On the charter's named LiteLLM fork (in-process vs proxy): the
proxy container buys central key custody, per-caller quotas/budgets, and one audit point *across
multiple consumers*; it costs another always-on service plus exactly the supply-chain surface this
finding warns about. With one API process and one agent, **in-process wins today**; the revisit
trigger is a second independent consumer of the gateway (at which point ADR 0092's own
proxy-promotion trigger fires). **Cost:** pinning/review-cadence = days; §4c-as-PRs ≈ 1–2 gate
weeks when first needed (Phase 5). **Unlocks:** the differentiator becomes demonstrable (a real
gated-promotion audit trail) instead of aspirational.

### F9 (SHOULD, cheap — MUST at first external release) — Pick a license

**What.** Decide and commit a LICENSE. Recommendation if any commercial path is plausible:
**AGPLv3 + CLA** (category-legitimate — Grafana thrives on it, and Elastic returned to open source
*via* AGPL in 2024 after the SSPL detour; deters closed SaaS forks; preserves dual-licensing). If
pure community: Apache-2.0. Also: a
source-by-source **data-licensing/redistribution audit** obligation now exists (the freedoms doc
names it), and OS-Pairs (CC BY-NC) must be fenced from any commercial artifact.

**Why.** No LICENSE = all-rights-reserved: nobody may legally use, fork, or contribute; it
forecloses exactly the community the build-in-public plan wants. It also silently blocks Track 3
(posting code snippets publicly while unlicensed invites cargo-culting you can't govern). **Cost:**
hours (decision) + the audit over time.

### F10 (MUST before scale/commercial; SHOULD start now) — Ethics, lawful basis, abuse-resistance

**What.** Treat compliance as schema and gates, not documents:
1. **Lawful basis as data:** a Legitimate-Interest-Assessment reference becomes a **required
   connector-manifest field** — a source cannot be enabled without a documented Art 6(1)(f) basis
   and a retention class; Art-9 special-category properties require an elevated basis tag. (Fits
   your existing manifest + JSON-Schema pattern exactly.)
2. **DPIA now.** An ER graph that matches/combines datasets, systematically evaluates people, and
   does "invisible processing" trips multiple WP29 criteria — effectively mandatory, and cheap
   while the system is small. Erasure (ADR 0049) is a *remedy*; it is not a *basis* — the charter's
   framing is correct and worth internalising in docs/00.
3. **Art 14 plan:** the disproportionate-effort exemption is read narrowly (*Bisnode*: cost alone
   doesn't qualify); the fallback is a public transparency/privacy notice + safeguards — publish it
   *before* the build-in-public posts.
4. **Query-audit log:** who viewed/queried which person, when — reads about persons are personal-
   data processing too, and insider misuse is the top realistic abuse vector for an intelligence
   tool. (As statements in the F1 log, it inherits the audit machinery.)
5. **Poisoning threat model:** your merge-independence requirement already resists single-source
   injection; add a correlated-source detector (coordinated feeds pushing the same claim/merge) and
   per-source anomaly flags (claim-flooding on one subject). Machine-extracted statements never
   anchor (F6). Provenance-diversity scoring on any surfaced claim.
6. **Hard red lines, written down:** no biometrics/face collection, no location-broker data, no
   fake-persona collection, no individual predictive-policing outputs (AI Act Art 5 — in force
   since Feb 2025). AI-Act posture: Annex III high-risk obligations are postponed to **Dec 2, 2027**
   (Digital Omnibus — treat as adopted; only OJ publication pending), Art 50 transparency applies
   from Aug 2, 2026; person risk-scoring becomes squarely high-risk **when used by/for law
   enforcement or migration** — so an *Annex-III assessment gate before any LE/gov sale* is the
   control to install, and self-hosting does not shed provider duties once you place it on the
   market.
7. The **active-tool posture is already right** (per-run scope token, sandbox sidecar, never
   agent-run, driver refuses) — the abuse-precedent research (Voyager Labs, ShadowDragon, Clearview,
   Babel Street) shows every blow-up came from covert/active collection, biometrics, location data,
   or marketing that named surveillance targets. Your invariants map onto every failure mode; that
   is a marketable asset (Track 3), but only if the claims stay true (F5).

**Cost:** LIA-field + manifest gate ≈ days; DPIA ≈ days of operator time with a template; query-audit
≈ small gate; the rest is policy writing. **Unlocks:** the commercial option at all; the
build-in-public posture ("we did the DPIA before we had users" is a differentiator in this
category); insurance against the category's known failure modes.

### F11 (COULD) — Smaller keeps, swaps, and hedges

- **Keep Splink + nomenklatura over Senzing** (the strongest managed/embedded alternative — real-time
  incremental by design, SDK v4, ~$59k/yr at 10M records, free ≤500). Reasons: closed-source
  resolution is philosophically and practically at odds with an auditable merge guard (the guard
  would wrap, not inspect); pricing at OSINT scale; and the OS-Pairs result shows near-frontier
  boundary adjudication is available locally anyway. **Revisit trigger:** sustained volume makes
  even hybrid micro-batch untenable (≈10–100× current).
- **Keep FtM — and pin it.** Write the missing dependency policy ADR: pin exact version, vendor the
  schema YAMLs (they're data), define an upgrade cadence + a schema-diff gate, and a `wm:` extension
  registry with the ADR-per-extension rule you already have (docs/20 §4). Also reconcile `wm:Article`
  (docs/20 proposes it; Phase 2 shipped FtM `Article`). L2's foundation being unpinned (digest §12.6)
  is a real but cheap-to-close exposure.
- **F11-T (this bullet: SHOULD, unlike the rest of F11 — it is exec-ranked #7): add
  `scope`/`workspace` to the new F1 tables from row one** (default `'default'`), and nothing else
  multi-tenant. The full RLS/per-workspace machinery stays unbuilt until a real second tenant
  exists. The register asks for the reversal cost to be *quantified*, so: re-introducing tenancy
  against the **current** shape is a fresh build across the surface ADR 0042 deleted (~110–120 call
  sites, an 8-table NOT NULL column, Neo4j composite keys/constraints, auth predicates on every
  route) **plus** what 0042 never had to do — an RLS/predicate design and a data migration of a
  *populated* graph and queue — realistically **4–8 gate-weeks and a person-affecting migration
  gate**, i.e. roughly a phase, not a gate. After F1-with-scope-column, the same capability is
  **~1–2 gate-weeks** (auth-token scope claims + API predicates + per-scope projections; the SoR
  already carries the column and projections rebuild per-scope). That asymmetry — a column now vs a
  phase later — is the entire argument, and it is also why "don't build multi-tenancy now" (§4c's
  managed-single-tenant commercial shape) is *earned* rather than assumed: the option stays cheap
  without building the feature.
- **Keep Zitadel** (fine, self-hosted, single-tenant); a managed IdP matters only if multi-org SaaS
  returns. **Keep MinIO**, add object-lock/versioning when cloud lands (S3/R2 portable by
  construction). **Keep the sandbox sidecar** (ADR 0077) — managed container sandboxes only beat it
  if you leave the single host.
- **Keep the 4-tool read-only MCP discipline**; grow it toward analyst nouns (F4) rather than raw
  Cypher. The deferred "raw Cypher for trusted/admin" open decision: resolve as **parameterised
  query templates** (a DSL you can bound and audit) rather than raw Cypher even for admin — cheaper
  to secure, and the projection (F1) makes ad-hoc Cypher in a *scratch clone* the safe escape hatch.
- **GDS:** the Community 4-core cap is acceptable for Phase-5-scale analytics on a projection; the
  inversion (F1) makes a later engine swap (or Aura Graph Analytics sessions) a projector target
  change, not a migration. The KuzuDB story (archived Oct 2025 after Apple acquired the company;
  forks unproven) is the cautionary tale for betting the SoR on a single-vendor engine — another
  argument for boring-Postgres-as-record.
- **Drop the `improvement/` stub** from the tree or fill it with the F8 §4c mechanism doc — a
  one-line docstring package that names the differentiator invites the "aspirational" critique.

### §2.13 (Charter dim 8) — Sequencing: the next five moves

1. **Truth-up sprint (F5) + license decision (F9).** Days. Do before any public post.
2. **The operator deploy** that is already the Phase-3 RESUME condition: S4 first Telegram brief
   *plus* the first real-seed calibration run (F3.1). This is the only move blocked on you rather
   than the fleet, and it unblocks two Musts at once.
3. **Review-queue UI (F4.1) in parallel with the statement spine (F1).** The review UI has **no
   hard dependency on F1** — it promotes the existing sign-off tables and CLI, so the fleet can run
   it alongside gate G-C. It goes this early because its value compounds: every week it doesn't
   exist, reviewer effort produces zero calibration labels. Statement spine: tables + dual-write →
   backfill → outbox/projector + rebuild-and-diff → cutover; ~3–5 weeks as a floor (F1's fold and
   backfill contingencies).
4. **Incremental ER + calibration v2 (F2 + F3.2–4)** on the spine, with the abstention band
   replacing the point threshold. ~3 weeks.
5. **Watchlists/diffs + dossier (F4.2–3)** — cheap once assertion-time exists — at which point you
   have: honest maxims, reversible merges, an error-rate-denominated threshold, a safety control
   that generates labels, and a demoable answer surface. Then Phase 4 opens with the extraction
   mapper (F6.2), which is also the moment to finalise the sovereignty repositioning (F6.1).

**Total-burden honesty:** the full program (G-A…G-H, Appendix B) sums to roughly **12–16 gate-fleet
weeks** of supervised calendar — a quarter, not a sprint, for one operator even with the fleet doing
the typing. If capacity allows only part of it, the cut-line is: moves 1–2, the review UI, and F1
steps 1–2 (statement dual-write + backfill). That subset already banks the audit substrate, the
label factory, and the honest public claims; everything else can resume later without rework. What
each deferral costs: deferring F1 steps 3–5 keeps the dual-write/DR exposure; deferring F2 keeps C4
false as a standing condition; deferring F4.2–3 keeps the product undemonstrable.

Everything in this sequence is one-operator-sized; nothing assumes a team. The two places a team
*would* change the answer are flagged in F7 (Temporal earlier) and Track 2 (managed-everything).

---

## 3. Track 2 — Clean-slate re-architecture

*Method note: I commissioned three separately-contexted designs (provenance-native prior;
infrastructure-first prior; product-led prior) before forming this synthesis. Where all three
agreed despite opposed priors, I say "3/3" — read that as a robustness check, not independent
evidence: the architects saw the same bundle (whose charter seeds several of these forks) and share
a model family, so correlated error is possible. Load-bearing positions below are argued on
external evidence, with the panel as corroboration. Their full texts are available on request; this
section is my design, informed by theirs.*

### 3.0 Thesis

I keep the seven core commitments (C1–C7) and challenge one framing: **the graph is the asset; the
answers are the product; provenance is the moat.** An FtM entity graph is table stakes — OpenSanctions
publishes one, OpenAleph indexes documents into one. What nobody in the category sells is *resolved
answers with receipts*: an alert, a brief, a dossier where every sentence cites an addressable,
source-attributed, time-stamped claim. That requirement — citability of every rendered sentence —
drives the whole design. C1 survives precisely: "many sources → one canonical graph" is right **as
the analyst's view**; it is wrong **as the storage primitive**.

### 3.1 Substrate & data model (Fork 1)

**A bitemporal claim journal is the system of record.** Two shapes:

```
statement(id, scope, subject, schema, prop, value, datatype,
          dataset/source_id, raw_ptr → s3, retrieved_at,
          valid_from, valid_to,          -- world time, as claimed
          asserted_at, retracted_at,     -- belief time, append-only
          reliability, origin{connector|llm@ver|analyst}, lawful_basis_ref)

decision(id, scope, kind{merge|split|negative|config}, member_ids[],
         evidence[stmt_ids], score, risk_tier,
         decided_by{model@ver|human:uid}, asserted_at, superseded_by)
```

in **managed PostgreSQL** (partitioned, PITR). An **entity is a fold**: statements grouped by the
canonical ID that the decision log currently assigns. Merges are belief revision — a decision row,
never a destructive write; a catastrophic merge is *repaired by a superseding decision*, and "what
did we believe on May 1" is a query (`asserted_at <= t`). The **canonical graph is a deterministic,
disposable projection** into a property-graph engine (Neo4j or successor — the engine is a
commodity once it's a projection), alongside two sibling projections: a search index and a pgvector
ANN space. Erasure = delete matching statements (+ hashed tombstones for audit-chain integrity),
crypto-shred raw objects, reproject.

**Why not the alternatives.** RDF-star/named-graph stores are the right *theory* of statement-level
provenance and the wrong *ecosystem* — you forfeit FtM/nomenklatura/Splink/GDS and buy SPARQL taxes
on every query (3/3 architects rejected; landscape research concurs). XTDB v2 (bitemporal SQL,
stable June 2025) is credible but young, single-vendor, and not a graph — you'd still build the
projection; boring Postgres does the bitemporal part with zero new vendors. Pure LPG-as-record is
the current design's founding limitation. FtM stays as **vocabulary, validation, and interchange**
(C2), pinned; the *internal* model is statements — which is FtM's own ecosystem-native deeper layer
(nomenklatura), so this is *more* ontology-first, not less.

### 3.2 Resolution (Fork 2)

**Incremental from day one, as a journal consumer; the judgement log is the ER state.** New
statements → candidate generation against the resolved index (canonical-anchor exact; fingerprint/
abjad blocks; ANN over multilingual name embeddings) → Fellegi–Sunter scoring with Splink-trained,
versioned weights → a **three-way decision per candidate**: auto-merge / abstain→review / reject,
with band edges set by an over-merge risk budget, per-schema. Batch Splink survives as the periodic
trainer, full re-clusterer, and drift auditor — never the gate. The guard's philosophy (independent
agreements, anchor-conflict as negative evidence, sensitivity fail-closed) carries over verbatim
(3/3 convergence with the current design) — but "alert vs block" cannot exist: the canonical view
folds only approved-or-auto decisions, so a pending sensitive merge *never materialises anywhere an
analyst or agent can read*. Blocked-but-plausible pairs materialise as `possibly_same_as` **links**
(leads, not verdicts, in the data model itself), so caution never hides signal.

**Cold-start calibration** (the charter's t=0 question): (1) canonical-anchor silver labels — the
current ADR 0079/0085 design, which all three architects independently kept (a genuine validation
of that work); (2) an external benchmark floor (OS-Pairs under commercial license, Febrl); (3) the
decisive mechanism: **review-as-labelling** — every human verdict from the review queue is a gold
boundary label acquired by uncertainty sampling, so the scarce reviewer hour compounds into
calibration exactly where the band lives; (4) LLM adjudication (local-capable at ~98% F1 on
compliance-style pairs) as pre-annotation only, never gold.

### 3.3 The LLM's place (Fork 3)

**Extraction is an ingest-path producer, frontier-by-default for public text.** text → FtM
statements with span-level provenance, model+prompt versioning, discounted reliability, and the
corroborate-never-anchor rule. The sovereignty threat model, stated honestly: the assets are analyst
intent, the aggregate graph, and subject personal data in context — not public newswire text. So:
one auditable egress gateway (converges with LiteLLM/ADR 0091's *mechanism*); **frontier under
ZDR/DPA as the default extraction lane; a local-mandatory lane** for queries, watchlists,
adjudication; an air-gapped profile as a deployable (and marketable) mode. Default-local as
*identity* is a brand position, not a threat model, and it costs the layer where the thesis pays
(3/3 convergence).

### 3.4 Tenancy & identity (Fork 4)

**Single-tenant-per-deployment product; multi-tenant-shaped data from row one.** Two statement
spaces: a **shared reference layer** (Wikidata/GLEIF/registries/sanctions — facts about the world,
resolved once, globally anchored — the yente model) and **per-scope overlays** (an org's sources,
judgements, watchlists, notes). Identity splits with it: canonical *anchors* are global; *belief*
(resolution decisions over non-anchored entities) is scope-local — two workspaces may legitimately
disagree about whether two Mohammeds are one person, and a bitemporal decision log per scope makes
that coherent. Even solo, `scope` = case-file/investigation (the Aleph "collections" insight).
Serious intelligence customers won't share a graph substrate anyway — **managed single-tenant
instances** is the commercial shape (the OpenCTI/Filigran pattern), which also keeps the vendor out
of the GDPR controller role for customer data. 3/3 convergence, and the sharpest lesson from the
current repo: ADR 0042 spent real effort *maximising distance* from tenancy at the exact moment the
constraint was about to lift. Founding choices should minimise the cost of being wrong about the
future, not encode the current constraint maximally.

### 3.5 Durability/HA spine (Fork 5)

**Postgres-as-log + Temporal Cloud from day zero; no broker.** The journal makes dual-write
disappear (one transactional write domain; projections are async, idempotent, rebuildable — the
outbox *is* the log). Durable workflows (ingest, map, resolve, project, erase, backfill, review
SLAs, promotion pipelines) run on Temporal — retries, heartbeats, exactly-once-effect cursors, and
multi-worker HA replace the four ADRs of hand-rolled scheduler resilience (0054/0074/0075/0059) the
current design accreted. Kafka/Redpanda only when multiple independent consumers or firehose rates
demand it — a wire-in behind the same log contract, not a rewrite. HA/DR = managed Postgres PITR +
versioned S3 + stateless workers + projections whose RTO is replay time. (Team note, honestly: this
assumes cloud services and ~$100+/mo of Temporal; for a strictly solo self-hosted operator the
Track-1 answer — advisory-lock lease + outbox + DBOS — is the same *shape* with lower ops, and that
is exactly why the two tracks diverge here.)

### 3.6 Agent runtime & mechanised self-improvement (Fork 6)

**Thin, owned loop; no general self-improving runtime at the core.** The agent jobs are workflows
with an LLM inside — scheduled briefs, investigation assistance, review pre-triage — ~500–2000 lines
on Claude Agent SDK or PydanticAI over the same MCP contract (the 4-tool discipline converges),
with pinned versioned prompts and full trajectory logging. Chat-platform delivery (Telegram) is
commodity glue. **Self-improvement is data + CI, not runtime magic:** a proposal is a signed config
`decision` in the journal (or equivalently a config PR); evaluation runs the metric harness against
a **shadow projection** (cheap, because projections are rebuildable — evaluate a proposed threshold
by folding with it); a **gate-policy table** maps risk class → auto / sampled / human-mandatory;
promotion is a versioned config event; rollback is a supersession. Person-affecting classes carry a
workflow step that cannot complete without a human signature — C6 as physics, not policy. 3/3
convergence on "don't put the riskiest subsystem inside third-party machinery."

### 3.7 Human-in-the-loop at scale (Fork 7)

**Risk-budgeted, sampled, tiered — and review is the label factory.** Every automated decision gets
`R = f(sensitivity class, person-affecting?, blast radius ≈ degree, margin, evidence diversity)`.
Tier 0 (below budget): auto-apply, 100% logged, 2–5% randomly sampled into retrospective review —
the sampled false-discovery rate is a control signal that tightens/loosens the budget. Tier 1:
queued with an SLA. Tier 2 (person-affecting, sensitive, high-centrality, thin-margin): hard stop,
dual evidence, human signature (C6 intact). Two properties the current design lacks: **when review
debt exceeds SLO, thresholds tighten automatically** (degrade conservative — a queue that silently
backs up is a de facto auto-approve in disguise); and every verdict feeds calibration, so the solo
reviewer is never pure overhead. A solo reviewer *is* viable under this design — because effort
scales with risk, not volume.

### 3.8 Consumption designed first (Fork 8)

The analyst's atomic objects: the **claim** (evidence), the **dossier** (entity as claim timeline
with contradictions surfaced), the **diff** (what changed since t — a range scan over assertion
time), the **lead** (ranked hypothesis + confidence + evidence chain — C5 as UI), and the **review
item**. Build order: review queue → watchlists/diff alerts → briefs (every sentence cites
`statement_id → raw_ptr`) → dossier → explorer last. The API's nouns are these; REST + MCP mirror
each other; SSE change feeds and saved-queries-as-alerts from day one. This ordering *forced* the
bitemporal substrate (diffs and as-of are the killer retention features and they are storage
properties) — which is the correct dependency direction: the surface dictated the schema.

### 3.9 Legal basis & abuse-resistance as architecture (Fork 9)

As in F10 but founding: `lawful_basis_ref` is a **required column**, ingestion fails closed without
a per-source LIA record in the manifest; purpose and retention tags enforced by TTL compaction;
per-source encryption keys in the raw zone (crypto-shredding); query-audit as statements;
correlated-source poisoning detection; no-single-source and no-machine-only rules for any
person-affecting render. The compliance reviewer's question changes from "show me your policy" to
"here is the column and the failing test."

### 3.10 Convergence/divergence map

| Fork | Current design | Clean slate | Verdict |
|---|---|---|---|
| Layering & the L2 contract | L0–L9, ontology as contract | Same shape; contract deepens to claims | **Converge** — the layer model is right |
| Ontology | FtM 4.x + STIX + `wm:` | Same, pinned, as vocabulary/interchange | **Converge** (best decision in the stack) |
| System of record | Neo4j LPG, merged entities | Bitemporal claim journal (Postgres) | **Diverge — the load-bearing difference** |
| Graph | System of record | Disposable projection (engine = commodity) | **Diverge** (consequence of above) |
| Provenance | Flat `prov_*` + witness map; Tier-2 deferred | Statement-native; per-claim by construction | **Diverge** (dissolves, not improves) |
| Merges | Destructive + guard + sign-off | Belief revision; non-destructive by construction | **Diverge**; guard *philosophy* converges |
| ER engine | Splink + nomenklatura, central L3 | Same engines, same central placement | **Converge** (placement is core-correct) |
| ER cadence | Batch windows | Incremental + periodic corrective re-cluster | **Diverge** |
| Threshold | Expert 0.92 point | Per-schema abstention band, FDR-denominated, review-fed | **Diverge** |
| Silver labels / benchmark floor | ADR 0079/0080/0085 | Kept nearly verbatim | **Converge** (re-derived 3/3 — a strong robustness signal for that work) |
| Raw landing zone | MinIO before mapping | Same (+ per-source keys, object-lock) | **Converge** |
| Plugin framework | Manifest + JSON-Schema, modes/capabilities | Same (+ LIA/retention manifest fields) | **Converge** |
| Task spine | asyncio + task table + lock | Temporal (Cloud) + journal cursors | **Diverge** (Track 1 takes the cheaper half-step) |
| Dual-write | Idempotency, Neo4j-first | Cannot exist (one write domain) | **Diverge** (dissolves) |
| API/MCP | Bounded 4-tool read surface | Same discipline; nouns become analyst objects | **Converge**, then extend |
| Agent runtime | Adopt Hermes | Thin owned loop | **Diverge** (Track 1: keep at edge, own the risky parts) |
| LLM posture | Local-default identity, 3-mode selector | Frontier-by-workload; local-mandatory lanes; one egress gate | **Diverge** on default; **converge** on the choke-point mechanism |
| Extraction | Not in ingest path (Phase 4 deferred) | Founding ingest-path producer | **Diverge** |
| Tenancy | Single-tenant, torn out (ADR 0042) | Single-tenant product, scope column + reference/overlay split from day 0 | **Half-diverge** (shape, not product) |
| HITL | Sign-off CLI, one queue, no tiers | Risk-budgeted tiers, sampling, review-as-labels, degrade-conservative | **Diverge** |
| Self-improvement | Designed contract (4a/4b/4c), unbuilt | Config-as-data through CI/journal; 4b dropped | **Converge on gates, diverge on mechanism; 4b: drop** |
| Consumption | 4 tools + config page; explorer "later" | Review queue/watchlists/briefs/dossier first; explorer last | **Diverge** (build order inverted) |
| Compliance | Erasure endpoint + gating discipline | Lawful-basis/retention/audit as schema | **Diverge** (bolt-on → founding) |
| Safety spine (C5/C6) | Leads-not-verdicts; human sign-off; gated | Identical commitments, stronger enforcement substrate | **Converge — non-negotiable, correctly held** |

**The headline of the map:** the current design's *judgement* — ontology, ecosystem, central ER,
guard philosophy, bounded surfaces, safety spine — converges almost everywhere with a clean slate;
its *storage inversion* and everything downstream of it (cadence, reversibility, provenance depth,
consumption order) diverges. Which is exactly what Track 1 F1–F4 fix in place. The clean slate is
not a different product; it is this product with the record and the view swapped — strong evidence
that evolving the repo (Track 1) dominates rewriting it.

### 3.11 What's most likely wrong in my design

**The projection is my single point of hand-waving.** All three architects flagged versions of the
same risk and so do I: deterministic fold semantics (conflicting valid-times, retraction ordering,
decision supersession) are the hardest engineering in this design, and if incremental projection
maintenance under revisable merges gets gnarly — an un-merge ripples through cluster-derived edges,
GDS projections, downstream scores — the team will start caching and *mutating* the projection,
quietly recreating graph-as-record with two half-truths instead of one. Mitigations, non-negotiable:
fold determinism is an invariant with property tests from day one; a scheduled
**full-rebuild-and-diff job** whose failure pages the operator; and a rule that the projection has
*no* write path except the projector. If that diff job is ever disabled, the design has failed.
Secondary risk: review-as-labelling assumes the queue gets operated; if the operator stops
reviewing, calibration starves silently — hence the degrade-conservative coupling in §3.7.

---

## 4. Track 3 — Communication

### 4a. Outward positioning

**The core critique of the drafts:** both lead with the commodity ("many sources → one graph with
provenance" — Aleph and OpenSanctions readers will shrug: *we have that*) and bury the four things
that are genuinely rare — **(1) calibrated, guarded, human-signed entity resolution as an explicit
product promise; (2) provenance doubling as the GDPR/audit log; (3) gated self-improvement with a
hard human boundary; (4) the adversarial multi-agent build method with property-tests-as-gate.**
Meanwhile several phrases overclaim against shipped state — and, ironically, the drafts *underclaim*
the one place you're strongest (the merge guard is block-by-default with a durable sign-off trail —
after F5's truth-up you can say "never auto-merge a sensitive entity" with a code citation).

**Claim-vs-reality table (use it as the copy filter):**

| Draft claim | Reality (2026-07-04) | Honest strong framing |
|---|---|---|
| "never an automated accusation" / leads-not-verdicts | True by design; scoring layers not yet built | Keep — say "by construction," note scoring ships later |
| "never auto-merge a sensitive entity" (implied) | **True** — block default (ADR 0031, settings.py:83) + sign-off queue | Say it *louder*, with the receipt |
| "provenance on everything… the audit/GDPR log" | Fail-closed on every node/edge; per-claim lineage on merged nodes = witness map; statement-level deferred | "Every node and edge carries fail-closed provenance; per-claim lineage is the current engineering frontier" |
| "de-duplicate before you count" | True within a batch window + for anchored entities; cross-batch gap for fuzzy long tail | "Resolved continuously against canonical anchors; cross-batch probabilistic resolution is the build in progress" |
| "calibrate before you conclude" | Harness built (B³/CEAFe/over-merge), never run on a real corpus; 0.92 expert-set | "A calibration harness is built; first real-corpus calibration is the next milestone" — do **not** say "calibrated" yet |
| "the resolved graph is the product" | Consumption = 4 read tools + a config page | "The graph is the asset; the first analyst surfaces are landing now" |
| "gated self-improvement… versioned, rollback" | Designed contract; 4a only; 4b/4c unbuilt | "Designed and enforced-by-contract; the loop ships gated, last, on purpose" |
| "93 decisions with reversibility classification" | Systematic from ~0054 | "Every decision since mid-June carries reversal cost + revisit trigger" |

**Reveal vs withhold.** Withhold: the source inventory (a collection-target list is an adversary
roadmap and future discovery evidence — the ShadowDragon lesson), the sensitivity taxonomy/denylist
mechanics (gaming surface), active-tool specifics and allowlist mechanics, per-source configs, the
repo itself until F5+F9 are done. Reveal, prominently: the ethical posture (leads-not-verdicts,
block-by-default guard, human sign-off, erasure), the ontology/standards choices, the ADR/gate
method, and the *existence* of active gating (abstracted). Get ahead of three landmines **before**
posting: (1) **claude-headless** — kill the mode (F6.1); "consumer subscription used
programmatically" is a gift to the first hostile reader; (2) **active scanning** — never name nmap
in marketing; have the one-paragraph gated/sandboxed/never-agent-run answer ready; (3) **personal
data** — publish the transparency/privacy notice and the DPIA-done statement *first* (F10.3), so
the answer to "you're building a people-graph?" is a link, not a scramble.

**Method-vs-product separation.** Run two tracks with different centres of gravity: a **build-log
track** (the gate fleet, property tests, ADR discipline, reversibility classification — aimed at
the engineering/AI audience; this is genuinely novel material and your strongest organic-reach
content) and a **product track** (what an analyst gets: answers with receipts — aimed at
OSINT/journalism/compliance). Cross-link, never blend: an investor or analyst reading "AI co-built"
as the headline will discount the product; an engineer reading product copy won't find the method.
Your Anthropic-vetting context is credibility for the *method* track; use it as biography, not
endorsement.

**Rewritten Option A (technical-professional, honest, differentiated):**

> Most monitoring tools hand you a feed. Feeds lie by repetition: the same shell company shows up
> under six spellings in four sources and suddenly it's "six data points."
>
> For the past few months I've been building **WorldMonitor** — an OSINT/geopolitical-intelligence
> platform whose core bet is narrower and harder than "we have a graph": **nothing should be
> counted until it's resolved.** Every source is mapped to an open ontology (FollowTheMoney +
> STIX — the same schema OpenSanctions and Aleph speak) and anchored to canonical identifiers
> (Wikidata, LEI, company registries). A probabilistic engine resolves duplicates across scripts
> and sources — continuous cross-source resolution for the fuzzy long tail is the build in
> progress, and I'll say so rather than pretend it's done.
>
> The part I care most about is what happens when resolution might be *wrong*:
> • A **catastrophic-merge guard** blocks — not logs, blocks — any merge touching a sensitive
> entity until a human signs it. That's the default in code, not a policy doc.
> • **Every node and edge carries fail-closed provenance** (source, time, reliability, raw
> pointer); that trail doubles as the audit log, cross-store right-to-erasure is implemented, and
> per-claim lineage across merged identities is the current engineering frontier.
> • Attribution and risk come out as **ranked hypotheses with confidence for a human to review** —
> leads, not verdicts. There is no "verdict" output format.
> • A calibration harness (B³/CEAFe/over-merge-rate, silver labels from canonical anchors, an
> external benchmark floor) exists so the merge threshold becomes a measured error rate, not a
> vibe. First real-corpus calibration is the next milestone — I'll publish what it says, either way.
>
> It sits in the gap between OpenSanctions (curated data, no platform) and Aleph (documents, no
> entity resolution): the **resolution layer with receipts**.
>
> The build method deserves its own post: the platform is co-engineered with Claude through an
> adversarial gate — independent test-authoring, implementation, and a skeptical reviewer that
> tries to break each change before merge, with property-based tests mandatory on anything touching
> a person-affecting invariant, and every decision recorded with an explicit reversal cost.
>
> Early, self-hosted, deliberately unglamorous. More soon — including the numbers.

**Rewritten Option B (short hook):**

> OSINT tooling mostly optimises for *more* — more feeds, more hits, more dashboards. I'm building
> the opposite bet: **WorldMonitor**, a platform designed so that nothing gets counted until it's
> resolved to a canonical entity. Every fact carries its source and reliability, and any merge that
> touches a sensitive person is blocked until a human signs it — that's the code default, not a
> policy. Attribution comes out as ranked hypotheses for review, never verdicts. Co-engineered with
> Claude under an adversarial test-and-review gate; every architectural decision carries a written
> reversal cost. The graph is the asset. The answers — with receipts — are the product.

**Rewritten Option C (one-liner):**

> Building WorldMonitor: an OSINT platform built on one rule — don't count it until it's
> resolved. Canonical entities, fail-closed provenance on every fact, sensitive merges blocked
> pending a human signature, attribution as reviewable hypotheses, never verdicts.

(Notes: all three now lead with the contrarian *quality* claim rather than the commodity graph
claim; the dedup claim is framed as the design rule ("nothing *should be* counted…", "designed so
that…") with the cross-batch gap explicitly disclosed in A, per the claim-vs-reality table above —
re-run any post through that table before publishing, and once F2 ships, the present-tense version
becomes honest; comparables are named in A so differentiation is legible; the AI-method is one paragraph in A, one
sentence in B, absent from C — matching the operator's own instinct.)

### 4b. Inward / design communication

The discipline is real and differentiating — byte-identical ground-truth mirrors *enforced by a
test*, property-tests-as-gate, reversal costs on recent ADRs, a roadmap with behavioural acceptance
criteria. The integrity risks are specific and fixable:

1. **Index/status/ledger drift** (detailed in F5) — the corpus's biggest credibility leak. A
   newcomer *cannot* currently reconstruct "why" from the ADRs alone past #0035 without filename
   archaeology; the Gate Ledger contradicts itself; ~20 merged ADRs read PROPOSED. Fix once, then
   make it a merge-checklist item. The strongest proof this matters: **your own review bundle got
   the guard mode wrong** — drift already misled the system's most careful reader (you).
2. **Self-classified reversibility is a real audit gap.** The planner agent writes the ADR including
   its own reversibility/person-affecting tags; checker and judge verify code and tests, not the
   classification; ADR 0047 self-declared "person-affecting: NO" and 0053 self-declared "no human
   fork needed." Mitigation is cheap: add "verify the ADR's reversibility & person-affecting tags
   against the diff" to the checker/judge mandate, and require a human co-sign line on any ADR that
   self-tags Sens or waives a human fork. High-stakes ADRs already record explicit user
   authorization (0042, 0045, 0091) — formalise that as the rule rather than the habit.
3. **Two-tier truth (MEMORY vs docs).** Live state lives in the operator's session memory
   (resume conditions, "what's next") while docs/ carry the formal record — and the roadmap lags
   merged reality. Adopt one rule: **at every gate park, durable state moves to the repo**
   (roadmap tick + ledger row); memory keeps only pointers. Otherwise the repo is not the system of
   record for its own project state — an ironic inversion for this particular product.
4. **Roadmap honesty for readers.** With Phases 4–6 unstarted, add a one-screen maturity table
   (layer → built/partial/deferred, auto-derivable from the status tags you already mandate) to the
   README, so a Phase-3 demo reads as "3 of 6, on plan" rather than as the ceiling.

### 4c. Product & market frame

**The landscape (verified June–July 2026):** OpenSanctions — bootstrapped, profitable-shaped,
open code + CC-BY-NC data + commercial data licenses (€0.10/call API); it sells *data*, not a
platform. Aleph fractured: OCCRP's **Aleph Pro** went proprietary (Dec 2025), the community fork
**OpenAleph** (DARC) does document-search, not ER. **Filigran/OpenCTI** — $58M Series C (Oct 2025),
the category's open-core success: open platform, paid enterprise/SaaS/AI. **Maltego** — PE-owned,
~$6.6k/yr pro tier, desktop link-analysis. **Linkurious** — acquired by Nuix (closed Apr 2026) at
~€12.5M upfront on ~€7M ACV (≈1.8–2.9× with earnout) — a sobering multiple for profitable
graph-investigation tooling. **Recorded Future** — Mastercard, $2.65B. **Palantir** — the ceiling,
not a comparable. New entrants (IVIX $60M et al.) are LLM+graph verticals.

**The open slot is real and specific:** *an FtM-native, self-hosted, resolution-first platform with
governed ER* — OpenSanctions sells data into it, OpenAleph indexes documents beside it, nobody
ships the resolved-graph-with-receipts layer. Three wedges, compatible and sequenceable:

1. **Governed-ER appliance** for screening/due-diligence/investigations — self-hosted or managed
   single-tenant (vendor stays out of the GDPR controller role; the Filigran deployment model).
2. **Open-core split per category norm:** open = engine, ontology tooling, connector SDK, community
   connectors, the safety controls (open-sourcing the *guard* is itself marketing); paid =
   governance/compliance pack (audit exports, DPIA/LIA templates, review-workflow UI at team scale,
   SSO/RBAC), curated connector/data packs, managed hosting, support.
3. **Non-person verticals first** (sanctioned-network mapping, corporate-structure graphs, CTI
   infrastructure) — revenue with minimal GDPR/AI-Act surface while the person-affecting machinery
   matures; person-risk modules ship later, self-hosted, customer-controlled.

Realism check, per the Linkurious multiple and your solo capacity: this category rewards
*durability and trust* over blitz; OpenSanctions' bootstrapped data-licensing model is the most
solo-compatible template. The strategic sequencing question (appliance-first vs open-core-first vs
stay-personal-tool) is Open Question 1 — it determines license (F9), OS-Pairs path (F3), and how
much of F10 is "now" vs "later."

---

## 5. Open questions for the operator

> **Answered 2026-07-04** — all seven decided by the operator; the durable record and the plan
> re-prioritisations are in **ADR 0094** (`docs/decisions/0094-strategic-review-operator-decisions.md`).
> Headlines: commercial = none-or-open-core (AGPLv3 at public flip); zero-egress *claim* dropped
> (sovereignty = operator-discretion mode, local default stays); tenancy = none (F11-T declined);
> persona = CTI investigators / L3 SOC analysts (CTI enricher slice first); ER scale = build-deeper,
> no paid products ever (Senzing permanently out, Community-forever); review budget ≤5 h/week
> (sizes the abstention band); claude-headless retained until Anthropic breaks or prohibits it —
> resolving the F6.1 landmine from the claim side rather than the mode side.

1. **Commercial intent, 12-month horizon: none / open-core / appliance+services?** Determines: the
   LICENSE (F9 — AGPL+CLA if commercial-optional; Apache-2.0 if community-pure), the OS-Pairs
   posture (commercial license vs eval-enclave), how much of F10 is now-vs-later, and whether the
   Track 3 wedge analysis is actionable or academic.
2. **Sovereignty: identity or policy?** My recommendation is F6's per-workload routing (frontier for
   public text under ZDR, local-mandatory for queries/adjudication, air-gapped profile retained and
   *marketed*). But "zero egress by default" is currently a brand claim of the project; softening it
   is a product-identity decision only you can make. Decide before Phase 4, because the extraction
   layer's ceiling depends on it.
3. **Tenancy ambition: none / workspaces-as-case-files / managed multi-org?** Determines whether F1's
   `scope` column is a hedge (my default), a real feature (case files are genuinely useful solo), or
   the start of the reference-layer/overlay split (§3.4).
4. **Who is the first external user?** docs/00 names use-cases but no persona. Before F4 locks the
   consumption build order, pick one design-partner archetype (investigative journalist vs CTI
   analyst vs compliance screener) — their first-hour experience should arbitrate what "dossier"
   and "alert" mean. This is the highest-leverage decision no amount of architecture can make for you.
5. **The ER-scale fork, pre-decided:** if sustained volume ever makes hybrid micro-batch untenable,
   is the answer building deeper (incremental clustering on the statement spine) or buying Senzing
   (~$59k/yr at 10M records, closed-source guard-wrapping trade-offs per F11)? Writing the trigger
   and the choice down now (ADR) prevents deciding it under duress later.
6. **Review budget:** how many hours/week will you actually spend in the review queue? That number
   sets the abstention-band width, the Tier-0 sampling rate, and the degrade-conservative SLO
   (§3.7). It is a safety parameter and only you know it.
7. **Build-in-public timing:** posts before or after the truth-up sprint + claude-headless removal +
   privacy notice? (My strong recommendation: after — it is days of work and converts three
   landmines into three receipts.)

---

## 6. Appendix

### A. Target architecture after Track 1 (text diagram)

```
                        ┌────────────────────── consumers ──────────────────────┐
                        │ Review-queue UI · Watchlists/diffs · Dossiers ·        │
                        │ Telegram briefs (Hermes @ edge) · REST + MCP (bounded, │
                        │ provenance-carrying, analyst-noun tools)               │
                        └───────────────────────────┬────────────────────────────┘
                                                    │ reads
          ┌────────────── projections (derived · disposable · rebuildable) ─────────────┐
          │   Neo4j graph (traversal, GDS)   ·   search index   ·   pgvector ANN         │
          └───────────────────────────────────────┬──────────────────────────────────────┘
                                                  │ outbox → idempotent projectors
┌───────────────────────────────── SYSTEM OF RECORD (PostgreSQL) ─────────────────────────────────┐
│  STATEMENTS  (scope · subject · prop · value · source · retrieved_at · asserted_at ·            │
│               reliability · origin · raw_ptr → MinIO/S3 · lawful_basis_ref · retention)         │
│  DECISIONS   (merge/split/negative/config · evidence · decided_by human|model@ver · supersede)  │
│  + review state · task audit · config versions · query-audit                                    │
└───────────────────────────────┬──────────────────────────────────────────────────────────────────┘
                                │ append-only
   connectors (L1) → map (L2: FtM-validated CLAIMS w/ provenance; LLM-extraction mapper
   emits corroborating claims w/ span provenance) → ER (L3: incremental micro-batch +
   periodic corrective re-cluster · abstention band · guard=block · judgement log) 
   raw bytes → MinIO landing zone (immutable, per-source erasable)
```

### B. Sequenced migration plan (gate-sized)

| # | Gate | Content | Effort (fleet calendar) | Blocked on |
|---|---|---|---|---|
| G-A | Truth-up | F5 doc estate + F9 license + digest corrections | 1–2 days | — |
| G-B | Deploy & measure | Operator deploy → S4 brief + real-seed calibration run + sufficiency report (F3.1) | operator days | **you** |
| G-C | Statement tables | Schema (+`scope`) + dual-write fused StatementEntity at merge + backfill | 1–2 wks | — |
| G-D | Projection | Outbox → idempotent projector + scheduled rebuild-and-diff + cutover of graph writes; DR test = rebuild | 1–2 wks | G-C |
| G-E | Incremental ER | Persisted resolved index + find_matches micro-batch + within-window + nightly corrective re-cluster + pgvector blocking | 2 wks | G-C (better after G-D) |
| G-F | Calibration v2 | Mixture-FDR + abstention band (human-signed) + local-LLM boundary pre-annotation | 1–2 wks | G-B |
| G-G1 | Review-queue UI | Promote sign-off CLI to web UI; verdicts→gold labels | 1–2 wks | — (runs **in parallel with G-C**; no F1 dependency) |
| G-G2 | Watchlists + diffs | Diff alerts + dossier over assertion-time | 1–2 wks | G-D |
| G-H | Extraction | LLM→FtM mapper plugin (frontier lane via gateway, span provenance, corroborate-never-anchor) + selector reposition + claude-headless removal | 2 wks | G-B; F6.1 decision |
| then | Phase 5 | Scorers each shipping their eval substrate; §4c config-PR mechanism on first real proposal | — | G-F, G-H |

### C. Stack scorecard

| Component | Verdict | Note |
|---|---|---|
| FollowTheMoney + STIX + `wm:` | **Keep (pin it)** | Best decision in the stack; add version/vendoring policy ADR |
| Neo4j Community + GDS | **Keep — demoted to projection** | CE limits (offline-only backup, no RBAC/HA, GDS 4-core) become irrelevant once disposable |
| Splink + nomenklatura | **Keep — change cadence** | Hybrid incremental; judgement log = ER state; Senzing = written-down revisit trigger |
| PostgreSQL (+pgvector) | **Promote to system of record** | Statements + decisions + outbox; wire pgvector for ANN blocking |
| MinIO | **Keep** | Add object-lock/versioning at cloud step; per-source keys later |
| Redis | **Keep** (unchanged role) | |
| asyncio + task table | **Keep, harden** | Advisory-lock lease + fencing; DBOS Transact incrementally; Temporal only at multi-node (Cloud, ~$100/mo) |
| FastAPI + FastMCP (4 tools) | **Keep, extend** | Analyst nouns next; parameterised query templates over raw Cypher |
| Zitadel | **Keep** | Managed IdP only if multi-org SaaS returns |
| LiteLLM (in-process) | **Keep, pin+hash** | March 2026 supply-chain incident = the precedent; proxy-container only when >1 consumer |
| 3-mode confidential selector | **Reposition** | Per-workload policy; **remove claude-headless**; add first-party API mode |
| Hermes v0.17 → 0.18+ | **Keep at edge, contained** | Pin+hash, lag releases, skill-file review cadence; never in the §4c path; thin-loop fallback stays cheap via MCP |
| Sandbox sidecar | **Keep** | Right design for single-host active tools |
| Prometheus + promtool CI | **Keep** | OTel/Loki still correctly deferred |
| Docker Compose | **Keep** | K8s/Helm only with multi-node; managed Postgres is the first cloud move |
| Gate fleet + ADR discipline | **Keep — it's a product feature** | Add classification-verification to checker/judge mandate; docs-drift checklist at park |

### D. Evidence & verification note

Landscape claims in this review were produced by five research passes (graph substrates, ER,
durability/orchestration, agent/LLM, market/legal), each followed by an independent adversarial
fact-check against primary sources (vendor docs, repos, regulators, filings). Ledger: **30
load-bearing claims checked — 25 confirmed, 5 modified, 0 refuted.** The five modifications, none
recommendation-flipping: AuraDB/GDS tier nuance; AWS ER incremental-billing detail; Aleph Pro
timeline (announced Apr 2025, launched Dec 2025, free for nonprofit journalism); Linkurious deal
economics (~€12.5M upfront ≈ 1.8× ACV, up to €20M with earnout, closed Apr 2026); EU AI-Act Digital
Omnibus (Annex-III postponement to 2027-12-02 treated as adopted, OJ publication pending). This
document was itself adversarially critiqued by three checker agents (repo-accuracy,
charter-compliance, reasoning-quality) before delivery; all findings were incorporated. Key in-repo
citations: `settings.py:83` (guard default), ADR 0019/0026 (batch), 0018/0045 (provenance tiers,
StatementEntity fusion, rejected alternative D), 0031 (return-to-block), 0042 (tenancy teardown),
0044/0048 (durable IDs), 0079/0080/0085 (labels/floor), 0089–0093 (Phase 3). Track-2 panel (a
robustness check, per the method notes — not independent evidence): three separately-contexted
clean-slate designs with opposed priors agreed on the storage inversion, incremental ER,
review-tiering, thin-agent-loop, frontier-by-workload, and consumption-first ordering; they
disagreed materially only on Senzing-vs-Splink (1/3 for Senzing; rejected here per F11) and
Temporal-at-t=0 (adopted for Track 2, deferred in Track 1).
