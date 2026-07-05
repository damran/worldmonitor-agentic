# 80 — Fable consult: log capture vs thinner canonical form (the Gate-3b E2/E4 question)

- **Status:** DELIVERED (2026-07-05) — advisory input to Gate 3b planning; decides nothing by itself.
  Every recommendation below becomes binding only when adopted by a 3b-planning ADR.
- **Charter:** the committed "Fable consult at Gate 3b planning" (ADR 0102 D6: *"The E2/E4 exclusions'
  own revisit belongs to the committed Fable consult at Gate 3b planning"*; ADR 0100 E2/E4). Question
  as prepared: **"append-only log = SoR, graph = derived rebuildable projection; at the irreversible
  cutover, should the log CAPTURE E4 (source-dataset labels) + E2 (downstream enrichment) so the
  projection is 100% reconstructable, or is the projected graph a deliberately-thinner canonical
  form?"** Asked for: recommendation · reversibility/lock-in tradeoff · pre-cutover invariants ·
  E4-vs-E2 together-or-split.
- **Execution note:** delivered by a fresh Fable-5 session operating as the repo's main loop
  (2026-07-05), fed by a 3-reader evidence sweep over the tree at `db3b46b`, then adversarially
  verified by three independent skeptic lenses whose findings (1 CRITICAL, 4 HIGH, 9 MEDIUM, 6 LOW —
  all initial verdicts FIX_FIRST) are folded into this text (§8). This satisfies the FABLE-AT-3B
  commitment.

## 1. Answer in one paragraph

**Split E2 and E4 — they are different kinds of data and deserve opposite defaults.** E2 (anchors +
enricher output) is **resolution evidence**, not decoration: capture it into the log as first-class,
provenance-stamped claims **before cutover** — a **blocking 3b prerequisite**. E4 (the FtM `datasets`
node label) is **transport metadata with zero in-repo interpreting consumers**: adopt the fold's
source-derived `datasets` as the canonical definition (a deliberately-*different*, more honest form),
optionally capturing the upstream label as a cheap rider on the Gate-2b migration — worth doing
mainly because it deletes an exclusion from the equivalence guard, not because anyone reads the data.
Beyond the question as asked, the evidence forces **two further blockers of the same rank**. First,
**the log must be able to FORGET**: erasure today never touches the statement log, so a full rebuild
would *resurrect every erased claim* promoted since Gate 2a — and, symmetrically, **reprojection
alone cannot enforce forgetting on the live graph** (the projector only ever MERGEs and additively
`SET`s), so the erasure gate must scrub the log *and* keep a defined live-removal mechanism. Second,
**the human sign-off flow is an entire un-logged write path**: `signoff.approve()/reject()` writes
the live graph with no statement, decision, or ledger row at all — a rebuild would silently drop
every human-approved merge, the most protected content in the graph. Sign-off must be routed through
the SoR spine before cutover.

**The right target is not "100% reconstructable."** It is four narrower properties:

1. **Rebuild destroys nothing load-bearing** (⇒ capture E2; log the sign-off lane).
2. **Rebuild resurrects nothing erased** (⇒ erasure scrubs the log — flow *and* accumulated stock).
3. **Forgetting reaches the live graph by a defined mechanism** (the MERGE-only projector cannot do
   it; the direct erasure prune survives write-path retirement, or a swap-rebuild mechanic replaces it).
4. **The unverified surface at cutover is minimal and named** (every property excluded from the
   fold-vs-direct equivalence and the divergence measure is either eliminated by capture or carries a
   documented rationale + revisit trigger).

A projection may be *thinner* than the live graph only in **derived picks** (caption, labels,
`prov_*` representative) — never in **evidence**.

## 2. Evidence base (verified facts this consult stands on)

All claims verified against the tree at `db3b46b` (3-reader sweep + adversarial re-verification; §8).

### E4 — the two dataset axes are real, distinct, and *almost* redundant

- Seven of eight connectors hardcode `"datasets": ["<connector_id>"]` at map time (e.g.
  `plugins/connectors/opencorporates/connector.py:129`); the ingest runner independently builds
  `Provenance.source_id = f"{connector_id}:{dataset}".rstrip(":")` (`runner/ingest.py:135`). The two
  values **coincide exactly** for bluesky/feeds/geonames/opencorporates (whose config schemas have no
  `dataset` key at all); coincide **unless** the optional `dataset` config key is set for
  dig/nmap/whois; and **never coincide for opensanctions**, where the FtM label is the payload's own
  slug (e.g. `ie_unlawful_organizations`) while `source_id` is `opensanctions:<slug>` — the *only*
  connector whose label carries genuine upstream information (the upstream catalogue's own dataset
  taxonomy).
- The statement log stores `dataset = Provenance.source_id` per claim (`resolution/statements.py:87`
  via `merge.py:304,344-354`); the FtM label never enters the log. The fold therefore reconstructs
  node `datasets` from source-ids (`resolution/projector.py:189`) — self-consistent, just a different
  axis (ADR 0100 E4).
- **The node `datasets` property has zero in-repo interpreting consumers.** It is passed through
  verbatim to API/MCP responses (`graph/queries.py:20,33` → `api/graph.py:46,60`,
  `mcp/server.py:94,103`), read-then-excluded by the divergence guard (`resolution/divergence.py:86`),
  and **deliberately NOT used by erasure** (`graph/ops.py:106-113` derives dataset membership from
  `prov_witnesses` + `prov_source_id` only). Nothing in the repo filters, joins, or branches on it.
  *Honest scope limit:* external API/MCP clients (scripts, the Hermes agent) receive the field
  verbatim and cannot be inspected — so redefining it at cutover is a wire-visible semantic change
  even if no in-repo consumer exists (§5 carries the mitigation).
- On the live path a merged node's `datasets` is the **union of member labels** within the last
  writing batch's cluster fusion (`resolution/merge.py:390` + FtM `ValueEntity.merge`), then
  overwritten wholesale by later batches' `SET n += props`. FtM's default when the key is absent is
  the **empty set** (followthemoney 4.9.2 `entity.py:47`) — nothing depends on an implicit label.

### E2 — anchors are resolution evidence, provenance-naked, and mis-modelled by the guard

- Producers today: **two connectors set anchors at map time** (geonames → `geonames_id`,
  opencorporates → `opencorporates_id`; pure functions of the raw record) and **one enricher plugin
  exists** (`WikidataEnricher` → `wikidata_id`; pure when it copies the FtM `wikidataId` property,
  **network-backed via SPARQL otherwise**). Anchor values live in **entity context** (keys prefixed
  `wm_anchor_`, `ontology/anchors.py:33`), not FtM properties — and the statement fusion iterates
  `member.properties` only (`merge.py:296-297`), so **no anchor can enter the statement log
  regardless of call ordering** (structural, not an ordering accident).
  *Documentation defect found here:* ADR 0100's E2 parenthetical ("enrichment runs on the written
  entity **after** the statement dual-write, `pipeline.py:437` vs `:483`") is actually **backwards** —
  `enrich` at `pipeline.py:437` runs *before* `record_statements` at `:483`. Immaterial to the
  conclusion (the structural context-vs-properties fact is what bars capture), but the ADR text
  should be corrected at the capture gate so no implementer picks a capture point from the false
  ordering.
- **On the written node, anchors materialise as BARE property keys** — `wikidata_id`, `geonames_id`,
  `lei`, `opencorporates_id` (`graph/writer.py:191` via `get_anchors`, which strips the context
  prefix: `ontology/anchors.py:31,99-123`). **This makes the divergence guard's E2 exclusion dead
  code**: `resolution/divergence.py:85` excludes `prop.startswith("wm_anchor_")`, a key shape that
  never occurs on nodes (its property test uses a synthetic `wm_anchor_qid` key,
  `test_prop_projection_divergence.py:128`; ADR 0102 D6 records the same wrong shape). Net effect —
  the guard **compares anchor properties today** while the fold never reconstructs them
  (`projector.py:110`): on any anchored corpus (geonames/opencorporates are anchored at map time),
  every anchored live node counts as UNEXPLAINED. The bug **fails safe** (false alarms, not hidden
  rot) and *strengthens* the capture case: **the guard cannot go green on an anchored corpus until E2
  capture lands** (or the exclusion is interim-fixed to the bare key names). Filed as a repo defect
  (§8).
- **The live driver never wires any enricher** (`runner/driver.py:573-577` calls `resolve_pending`
  without `enrich`); only tests pass one. Production anchors today are map-time-only — so the set of
  production *enricher-derived* values is currently empty.
- **Enricher output carries no per-anchor provenance** — no method, no retrieved-at, no source stamp.
  A SPARQL-derived anchor is indistinguishable from a source-asserted one.
- Anchors are **load-bearing downstream**: a Splink negative-evidence comparison (anchor-clash), an
  input tier of the durable canonical id (`pick_anchor`), a catastrophic-merge-guard **park trigger**
  whose values surface verbatim in the review UI, Neo4j **uniqueness-constraint** targets, and
  implicit members of every API/MCP entity response. Two precision limits (per adversarial check):
  the ER loop reads anchors from **queue candidates**, not graph nodes, so a thin rebuild does not
  directly corrupt future ER runs; and **durable-id coherence is a forward-looking, not
  present-tense, loss** — `pick_anchor` derives ids from FtM identifier *properties*
  (`wikidataId`/`leiCode`/`registrationNumber`/`taxNumber`, `canonical.py:144-149,168-170`), which
  *are* statements and *are* folded, while the two map-time anchors are deliberately **not** id tiers
  (`canonical.py:119-121`). What a rebuild loses **today** is the bare anchor node properties — the
  uniqueness-constraint targets and the API/analyst surface; a `wm-anchor-*` id orphaned of its
  justification becomes possible only once the SPARQL enricher path is wired without capture.

### The sign-off write path — an entire un-logged production lane (discovered)

- `signoff.approve()` writes the canonical entity + edges to the **live graph** via `write_entities`
  (`resolution/signoff.py:290`); `signoff.reject()` writes member nodes + edges (`:342`). The module
  imports **none** of `record_statements` / `record_decision` / `record_durable_id` — the flow writes
  `SignOff` + `ResolverJudgement` audit rows, but **nothing the fold reads** (statement / decision /
  ledger). Under the default `MERGE_GUARD_MODE=block` (ADR 0031) this lane fires on exactly the
  **sensitive, person-affecting merges** the guard parks.
- Consequences: a full rebuild **silently drops every human-approved merge and every reject-written
  member node** — evidence *and* a human judgement, the most protected content in the graph; the
  alias⇔co-commit invariant (§7-6) does **not** catch it, because sign-off writes no ledger alias;
  the divergence guard reports these nodes as unexplained forever, deadlocking §7-10 on any corpus
  where a park+approve ever occurred; and an E2 capture hooked only at the *pipeline* promote point
  structurally misses sign-off-promoted entities.

### The erasure axis — the log cannot forget, and the graph cannot be made to forget by folding

- Erasure today (`graph/ops.py` + the erasure flow) deletes landing objects, redacts
  `er_queue`/dead-letter rows, and value-level-prunes Neo4j (sole-witness props `REMOVE`d,
  sole-source nodes `DETACH DELETE`d). The statement writers "only INSERT; no UPDATE or DELETE is
  ever issued" (`resolution/statements.py` module contract) — **no log-scrub code exists**. A
  `full_rebuild` fold therefore **resurrects every erased claim** promoted since the Gate-2a
  dual-write began, holding dangling `raw_pointer`s to deleted landing objects. ADR 0095 (:63)
  *declares* the target story ("value-level GDPR erasure = `DELETE … WHERE` + reproject") but the
  build has not reached it.
- **The converse limit (adversarial finding):** scrubbing the log does not, by itself, propagate to
  an already-written live graph. Three mechanisms: a `DELETE … WHERE` emits **no new `seq` row**, so
  the incremental projector (`WHERE seq > watermark`) never revisits the scrubbed survivor; even a
  full fold into the live graph writes via `MERGE` + additive `SET n += props`, which never removes a
  value, property, or node; and the only wipe in the codebase is the diff guard's, whose two-gate
  fence *structurally refuses* the live graph by design (ADR 0102 D3). **Reprojection enforces
  non-resurrection; it cannot enforce removal.**
- **Granularity mismatch (adversarial finding):** the live prune is *prop-granular* (a co-witnessed
  property keeps ALL its values, including erased-source-only values), while a log scrub is
  *row-granular*. Post-scrub, live value-sets can legitimately exceed the fold on ordinary compared
  properties (`name`, …) — permanent "unexplained" divergence unless the erasure gate reconciles the
  two granularities.
- Capture headroom already in the schema, **no migration needed**: `statement.method` (always NULL),
  `statement.scope` (unenforced), `decision.supersedes`/`superseded_by` (always NULL), and
  `decision.evidence` JSONB (*not* idle — it already carries `{"reason": …}` on every promoted merge,
  `statements.py:145`; additional keys are additive).
- The complete fold-irreproducible inventory beyond E2/E4: E1 (cross-batch resolution — structural,
  *desired*), E3 (`prov_*` representative shift — a derived pick), caption/labels (derived picks),
  the edge-side `datasets` prop (F4 family), the sign-off lane (above), and **zero-FtM-property
  promoted entities** — reachable in practice (`FtmBulkConnector.map` is near-identity and
  `validate_or_raise` requires only id+schema), written live today (provenance-stamped ⇒ passes G1)
  but yielding zero statement rows ⇒ **no node after a fold**.

## 3. The principle (why "100% reconstructable" is the wrong target)

The statement log is the system of record for **evidence and judgements** (ADR 0095). Three kinds of
thing appear on a projected graph node, with different contracts:

| Kind | Examples | Contract | Treatment |
|---|---|---|---|
| **Evidence** | FtM property values, witnesses, anchors, enricher claims, *entity existence itself* | Must survive rebuild; must be erasable at the SoR | **In the log** (capture E2; log the sign-off lane; §7-3 zero-prop disposition) |
| **Judgements** | canonical id (the E1 join key), survivor rewrites, merge/sign-off decisions | Must replay deterministically from decision/ledger rows | Already in the log/ledger — *except the sign-off lane (§6b)* |
| **Derived picks** | caption, label set, `prov_*` representative, source-derived `datasets` | Recomputed by the fold; may legitimately differ from any single live write | **Thinner canonical form is correct** — never capture; document + revisit-trigger each exclusion |

Two refinements the adversarial pass forced. (i) **A fourth category exists**: evidence that *is*
faithfully reconstructed by the fold but *excluded from the guard's comparison for v1 convenience* —
today exactly `prov_witnesses` (monotone-comparable, ADR 0102 D6-ii keeps a completeness-check
revisit). Convenience exclusions are debt with a revisit trigger, not derived picks. (ii) **Entity
existence is itself evidence**: a zero-FtM-property promoted entity is a live, provenance-stamped
node the fold cannot produce at all — it must be dispositioned (capture an existence claim, reject at
promote, or a documented exclusion), not left implicit.

E2 sits squarely in row 1 — the current design mis-files it as decoration. E4 sits in row 3 *except*
the opensanctions upstream slug, which is row-1-adjacent information with no consumer (§5). "100%
reconstructable" fails as a target twice: it would freeze derived picks that are *supposed* to be
recomputed (caption/E3), and it says nothing about the erasure direction — a log that reconstructs
everything forever is a **GDPR liability**, not an asset. Reconstructability must be bounded by the
right-to-forget: **the log is the SoR for what may be reconstructed** — so forgetting must be an
operation *on the log* (non-resurrection), paired with a defined live-removal mechanism (§6).

## 4. Recommendation E2 — capture, as first-class claims (BLOCKING 3b prerequisite)

**Capture anchor/enricher output into the SoR spine as provenance-stamped claims, and make the fold
reproduce anchors.** Not because "the graph must be 100% reconstructable," but because anchors are
evidence: they gate merges, feed the durable-id tiering, and back uniqueness constraints. A rebuild
that silently drops them amputates the product surface — and, per §2, the divergence guard is
*already* comparing them against a fold that cannot produce them, so capture is also what makes the
pre-cutover verification instrument usable at all.

**Shape (design sketch for the capture gate — final call belongs to that gate's ADR):**

- **A second append-only lane in the same spine** — a `context_claim` (or `anchor_claim`) table:
  `canonical_id · key · value · dataset · method · retrieved_at · seq` — written at **both** live
  promote points: the pipeline promote block *and* `signoff.approve()` (§6b; a capture hooked only at
  the pipeline point structurally misses sign-off-promoted entities). INSERT-only, same atomic
  co-commit discipline. The projector folds it into entity context before `write_entities`; ADR 0100
  D3's "Anchors — not reconstructed (E2)" note is retired.
- **Guard/test mechanics, stated precisely** (the adversarial pass corrected this): there is **no
  anchor exclusion in IT-PROJ-2 / P-FOLD to drop** — those exclude only `datasets`; anchors are
  handled by corpus *absence*. The work is: delete the **dead** `wm_anchor_` branch from
  `divergence._excluded` (`divergence.py:85`) — replacing it *until capture lands* with an honest
  interim exclusion of the bare anchor keys (`CANONICAL_ID_FIELDS`, `anchors.py:31`) if the guard is
  enabled on an anchored corpus first — and, at the capture gate, add **anchored corpora** to
  IT-PROJ/P-FOLD with anchor parity assertions.
- **Conflict semantics must be defined at the capture gate** (adversarial finding): the projected
  anchor node property is single-valued, so if the lane ever holds *conflicting* same-key claims for
  one survivor (a human-approved anchor-clash merge; upstream drift), the folded anchor becomes a
  **pick** over claims — exactly the caption problem (ADR 0102 D6-iii). Deterministic pick ⇒
  `wm_anchor` likely stays guard-excluded like caption; set-valued projection ⇒ comparable. Decide
  there; either is coherent, silence is not.
- **Provenance requirement rides along:** the capture gate REQUIRES `method` (e.g.
  `enricher:wikidata@<version>` / `connector:geonames`) and `retrieved_at` on every captured claim —
  closing the "provenance-naked anchors" gap in the same stroke (G1's spirit applied to enrichment).
- **Re-run-at-projection-time is REJECTED** as the E2 resolution: `WikidataEnricher` is
  network-backed (SPARQL) — re-running it inside the fold makes rebuild non-deterministic (the ONE
  named ADR-0095 risk), couples DR to an external service, and lets a rebuild silently rewrite
  history as upstream answers drift. Enrichment is an *ingest-time producer of claims*, never a
  fold-time step. (A one-time backfill **re-map** of landing-zone raw records is fine — that is 2b
  machinery, not fold-time enrichment.)
- **Rejected alternative — pseudo-prop rows in the `statement` table** (`prop="wm_anchor_*"`). The
  decisive ground: FtM validation — `get_prop_type` raises for a non-schema prop at `Statement`
  construction, and the fold's `make_entity` would need a prefix branch; anchors are deliberately
  *not* schema properties. (Two earlier grounds are withdrawn per the adversarial pass: `prop` is in
  FtM's `statement_id` hash preimage, so a disjoint namespace cannot collide; and only P-STMT-1's
  oracle — not "every P-STMT invariant" — would need extension.) **Booked cost of the separate
  lane** (what pseudo-props would have inherited for free): its own checkpoint watermark, the
  F3-style incremental touched-survivor full-history re-read, Gate-2b backfill coverage, and
  erasure-scrub coverage (§6 scopes the scrub over all three lanes). The lane still wins on the
  FtM-purity ground, but the ledger is two-sided.
- **Sequencing note:** since the driver doesn't wire enrichers today, the capture gate is also the
  natural place to decide enricher wiring (enrich → record claims → write), so enricher output is
  log-first from its first production run. Map-time anchors (the only production anchors today) are
  capturable at promote time immediately.

**Reversibility/lock-in (qualified per the adversarial pass):** additive (new table + fold
extension) — low reversal cost (stop writing the lane; fold ignores it). The lock-in asymmetry:
**once enrichers are wired, un-captured enricher output is unreconstructable-forever** (the
determinism argument forbids fold-time re-run) — so capture-before-enricher-wiring is the
ordering-critical edge. **Map-time anchors are re-map-recoverable** from the retained landing zone
(the same 2b path §5 relies on), so their capture is motivated by guard strength and the product
surface, not forever-loss. Revisit trigger: anchor volume or a context-shaped payload that doesn't
fit the `key·value` lane (would force a JSONB column, not a redesign).

## 5. Recommendation E4 — thinner canonical form, with a cheap optional rider

**Adopt the fold's source-derived `datasets` as the canonical definition.** The projected value
(deterministic set of the claims' source-ids) is **at least as informative for 7 of 8 connectors** —
*identical* for bluesky/feeds/geonames/opencorporates (no `dataset` config key exists), *strictly
richer* for dig/nmap/whois when the operator sets the key — and it is derived from data already in
the log. This is a deliberate, documented **semantic redefinition** at cutover, not a loss: record it
in the 3b ADR ("node `datasets` := source-id set of the folded claims") **and surface it at the wire**
(a response-schema description / changelog entry for the `datasets` field), since external API/MCP
consumers we cannot inspect receive the field verbatim and an ADR line is invisible to them.

**The one genuine loss** is the opensanctions payload slug (upstream dataset taxonomy). Three facts
make this acceptable as a default: (1) zero in-repo consumers today; (2) the raw payloads are
retained in the landing zone, so the slugs are **recoverable by re-map** (the Gate-2b machinery) if a
consumer ever appears; (3) if that consumer appears, the *right* home is a first-class claim (a
per-statement `origin_dataset` column or an FtM-property-shaped mapping), not the ftmg node-label
side channel.

**Optional rider (recommended if Gate 2b touches the statement row shape anyway):** add a nullable
`origin_datasets` (JSON list) column populated at fuse time from the member entity's FtM label set.
Cost: one column + one fold line (node `datasets` := union over rows). The payoff is **guard
strength, not data**: IT-PROJ-2's E4 exclusion becomes removable, and the divergence measure can
compare `datasets` under its live-⊆-fold subset rule. **Scope of the monotonicity claim (qualified
per the adversarial pass):** live ⊆ fold holds in the **no-erasure, no-zero-prop-member regime** —
a §6 log scrub removes labels from the fold union while the live node keeps them (`SET n +=` never
retracts), and a zero-FtM-property cluster member contributes its label to the live union while
writing zero rows. The erasure gate's granularity reconciliation (§6) is what restores the guard's
usability after real erasures; the rider decision should note both caveats. **Decide at 2b planning;
do not build a standalone gate for it.** The 2b backfill is also where the Gate-2a judge's open
`statement.dataset` stamped-ness question resolves (unstamped-member fallback to entity id — decide
nullability vs a stamped-ness assertion), so the canonised source-id axis is guaranteed pure.

**Reversibility/lock-in:** the redefinition is reversible pre-cutover (the direct writer still
exists) and *recoverable* post-cutover via landing-zone re-map — the reversal path 2b's backfill
exercises anyway. The rider column is additive/nullable; reversal = stop populating.

## 6. The forgetting prerequisite (erasure must reach the log — AND live removal must stay defined)

Before cutover makes rebuild the routine DR/verification path, erasure must satisfy **both
directions**:

- **Non-resurrection (the log side).** Value-level scrub on `statement` rows (the ADR-0095 promise:
  `DELETE … WHERE` + reproject), the matching treatment for `decision` rows referencing erased
  members (likely tombstone/redact rather than delete — decision rows are judgements; the erasure
  gate's ADR decides), **and the §4 `context_claim` lane** — all three lanes are scrub surfaces; an
  erasure gate scoped to statements alone would resurrect erased-source anchor claims on rebuild.
- **Live removal (the graph side) — reprojection cannot do it.** The projector only MERGEs and
  additively `SET`s; a log `DELETE` emits no `seq` row, so the incremental fold never even revisits
  the scrubbed survivor. The erasure gate must therefore pick a defined mechanism: (a) **keep
  `graph/ops.py`'s direct prune** as a permanent, explicitly-carved-out second live writer surviving
  "retire the direct write path" (§7-14); (b) **seq-bearing erasure-event rows** the projector
  consumes with delete capability; or (c) **wipe-and-full-reproject** on erasure (a swap-rebuild
  mechanic). Each is coherent; the current text of ADR 0095 ("`DELETE … WHERE` + reproject") is
  **not** sufficient as stated for an already-written live graph.
- **Granularity reconciliation.** The live prune is prop-granular; a log scrub is row-granular.
  Unreconciled, every real erasure leaves live value-sets exceeding the fold on *compared* properties
  → permanent unexplained divergence → §7-10 deadlocks. The erasure gate must align them (or the
  guard must learn to explain erasure deltas).
- **Stock, not just flow.** The log has been accumulating claims since the Gate-2a dual-write began
  (2026-07-04), including claims from **sources erased after logging** — a forward-looking scrub
  path leaves that window resurrection-ready. A **one-off retroactive scrub** of every erasure
  executed during the dual-write window (driven from the erase-audit records; e.g. `DELETE FROM
  statement WHERE dataset = <erased source_id>`) is part of the prerequisite.
- **The round-trip property must assert BOTH surfaces:** erase → (i) `full_rebuild` fold into a
  fresh target contains nothing of the erased source, **and** (ii) the **live graph** no longer holds
  the erased values. A fresh-target-only oracle goes green while the live graph still holds
  everything (the adversarial pass caught exactly this in this consult's first draft).
- **Fold-suite impact:** P-FOLD-2 (incremental == full) is proven under a no-deletion bound; the
  erasure gate must bound or extend the property (a scrub between batches legitimately breaks naive
  incremental-vs-full equivalence unless the erased survivor is re-folded or event-driven).
- This gate is **person-affecting** (it changes what an erasure actually erases) → human cosign
  under ADR 0097 — one more reason to keep it separate from the additive, non-person-affecting E2
  capture lane.

## 6b. The sign-off prerequisite (route the human lane through the spine) — discovered CRITICAL

`signoff.approve()/reject()` writes the live graph (`signoff.py:290,:342`) with **no** statement,
decision, or ledger-alias row — durably audited in `SignOff`/`ResolverJudgement`, but **invisible to
the fold**. Under the default guard mode (`block`, ADR 0031) this lane carries exactly the sensitive,
person-affecting merges. Before cutover:

- `approve()` must co-commit the same spine writes the pipeline promote block does — statement rows
  for the fused canonical (+ the §4 context lane) and a decision row with `decided_by=<operator>`
  (the human-decision path ADR 0099 explicitly reserved), plus whatever ledger write makes the
  survivor resolvable — or the flow is re-routed *through* the pipeline promote point. `reject()`
  needs the member-write equivalent.
- Note the alias⇔co-commit invariant (§7-6) does **not** cover this lane today (sign-off writes no
  ledger alias) — the two conditions are independent and both required.
- This change is squarely **person-affecting** (it alters the mechanics of the human sign-off path)
  → its own gate, human cosign, mandatory `@given` coverage per the build discipline.

## 7. Pre-cutover invariant checklist (what 3b planning should adopt as gate conditions)

**Evidence capture:**
1. **E2 capture live** (§4): anchor/enricher claims flow log-first from **both** promote points
   (pipeline + sign-off); the fold reproduces anchors; the dead `wm_anchor_` exclusion is deleted
   from `divergence.py` (interim: exclude the bare `CANONICAL_ID_FIELDS` keys until capture lands);
   IT-PROJ/P-FOLD gain **anchored corpora** with anchor-parity assertions; anchor conflict semantics
   decided (pick ⇒ guard-excluded like caption; set-valued ⇒ compared).
2. **Sign-off routed through the spine** (§6b): approve/reject co-commit statement + decision
   (`decided_by=<operator>`) + ledger rows, or re-route through the pipeline promote point.
3. **Zero-property promoted entities dispositioned**: capture an existence claim, reject at promote,
   or a documented exclusion — decided and tested; until then they are an un-captured evidence class.

**Forgetting:**
4. **Erasure round-trip green on BOTH surfaces** (§6): erase → rebuild-into-fresh-target contains
   nothing erased AND the live graph no longer holds it; scrub scope = all three lanes (statement,
   decision, context_claim); granularities reconciled; P-FOLD-2 bounded/extended for deletions.
5. **Retroactive stock scrub**: the dual-write-window's erased-source claims scrubbed once, verified
   by a rebuild-contains-no-erased-source check.

**Write-path integrity:**
6. **Alias⇔co-commit invariant asserted** (3a-ii-A HIGH backlog): any producer writing a ledger alias
   co-commits the survivor's statements (or the fold gains a completeness check). Independent of
   item 2 — sign-off writes no alias, so both are needed.
7. **Single-writer ingest asserted at cutover** (ADR 0100 D1's stated assumption): the incremental
   watermark is gap-safe only single-writer; assert/enforce it, or build the min-in-flight-seq
   watermark first. Post-cutover a violated assumption is permanent live loss, not a dormant-guard
   artefact.
8. **Superseded-node deletion has a decided owner** (ADR 0100 LOW backlog; ADR 0102 defers it OUT —
   3b is the last named owner): a projector/guard delete step, or documented acceptance of
   alias-on-read staleness with a revisit trigger.

**Verification instruments:**
9. **Gate 2b backfill landed** (pre-2a nodes in the log; also resolves `statement.dataset`
   stamped-ness, §5) — until then the guard honestly reports pre-log nodes as unexplained and cutover
   would orphan them.
10. **Divergence guard enabled and green over N cycles on the real corpus** — *explicitly dependent
    on items 1, 2, 4 and 9* (anchored/sign-off/erased corpora cannot go green before them), with the
    E4 decision (§5) reflected in the exclusion list.
11. **Exclusion-surface audit, split by instrument** (the two surfaces differ): the **divergence
    predicate** = `{id (E1 join key — a judgement, not a pick), caption, prov_* family (incl. the
    D6-ii witness-completeness revisit; prov_witnesses is a convenience exclusion, not a derived
    pick), datasets iff the §5 rider was declined, wm_anchor iff pick-semantics won item 1}`; labels
    remain **never-compared** per D6-i (its own rationale + revisit trigger). The **equivalence
    signature** (IT-PROJ/P-FOLD) compares labels and anchors and must keep doing so. Anything new on
    either surface is a new un-captured evidence class and blocks cutover.
12. **One-time two-directional / count-level reconciliation at the cutover moment**: enumerate every
    fold-side extra and explain it as E1; the D6 blind spots (same-id multiplicity — representable,
    since no uniqueness constraint exists on `n.id`; id-less elements; fold-side extras) are
    acceptable for the *recurring* guard, not for the irreversible gate.

**Operational:**
13. **3a-ii-B LOWs folded into the 3b driver work**: single ledger read, handshake-refusal
    observability, **and snapshot scale** (stream/batch the whole-graph reader — the guard/DR
    verification at production scale depends on it).
14. **Write-path retirement enumerates its carve-outs explicitly**: the erasure live-prune (§6
    option a) and the diff-guard's isolated-target writes survive; anything else that writes the live
    graph after cutover is a defect.

## 8. Adversarial verification (what it found, and what changed)

Three independent skeptic lenses (fact-check every citation; refute the architecture; attack the
checklist's completeness) reviewed the first draft. **All three returned FIX_FIRST** — 1 CRITICAL,
4 HIGH, 9 MEDIUM, 6 LOW — the fourth consecutive time this repo's adversarial-verify pattern has
caught real defects pre-merge. Every recommendation **survived** (each lens said so explicitly); the
mechanics did not. Folded corrections, by weight:

- **CRITICAL (completeness):** the un-logged sign-off write path — now §2, §6b, and checklist items
  2/10. The first draft's inventory claimed completeness while missing an entire production lane.
- **HIGH (facts):** the guard's `wm_anchor_` exclusion is dead code against bare anchor keys — the
  first draft proposed "dropping an anchor exclusion" that does not exist in IT-PROJ/P-FOLD and
  missed that the guard compares anchors today (§2, §4, item 1). **HIGH (architecture):**
  "reprojection as enforcement" was wrong for the live graph (MERGE-only, watermark blind to
  DELETEs) — §6 now demands a defined live-removal mechanism and a both-surfaces round-trip oracle;
  "unreconstructable-forever" was overstated for map-time anchors (re-map-recoverable; forever-loss
  applies to wired-enricher output); the §5 monotonicity claim now carries its no-erasure /
  no-zero-prop-member bounds. **HIGH (completeness):** the stock-vs-flow gap (item 5).
- **MEDIUM/LOW:** informativeness reworded (identical for 4, richer for 3-when-configured); ADR 0100's
  backwards ordering parenthetical flagged rather than endorsed; durable-id orphaning re-scoped to
  the forward-looking enricher path; pseudo-prop rejection re-based on the FtM-validation ground with
  the lane's duplicated machinery booked; taxonomy gained the convenience-exclusion category, the
  id-as-judgement correction, and the existence-is-evidence note; zero-consumers scoped in-repo with
  a wire-visible mitigation; exclusion surfaces split by instrument; items 7, 8, 12, 13, 14 added;
  `decision.evidence` headroom annotated.

**Repo defects discovered (actionable independent of 3b, filed to backlog):** (1)
`resolution/divergence.py:85` — the `wm_anchor_` prefix exclusion never matches a real node property
(bare keys per `anchors.py:99-123`); its property test uses a synthetic key; ADR 0102 D6's text
records the wrong shape. Dormant guard ⇒ not urgent; fix = interim bare-key exclusion + ADR-0102
erratum, or fold into the capture gate. (2) ADR 0100's E2 ordering parenthetical is backwards
(enrich at `pipeline.py:437` precedes the dual-write at `:483`) — docs erratum at the capture gate.
(3) The §6b sign-off lane itself — the largest, its own gate.

## 9. What this consult does NOT decide

- The capture-lane schema details, enricher wiring order, anchor **conflict semantics** (§4), the
  **erasure live-removal mechanism** (§6 options a/b/c), the decision-row erasure semantics, and the
  sign-off re-route-vs-dual-write choice (§6b) — those belong to the capture/erasure/sign-off gates'
  own ADRs (with this document as input).
- Whether Gate 2b takes the §5 rider — 2b planning decides when it sees the backfill migration.
- Anything about cutover mechanics (checkpoint promotion, retire-direct-write sequencing) beyond the
  checklist conditions — that is Gate 3b planning proper, which this consult unblocks.
