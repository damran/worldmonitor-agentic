# Gate E — Fail-Closed Sensitivity Guard

> **BUILD gate.** Closes audit gap **G6**. Inverts the denylist half of **ADR 0020** to
> deny-by-default. ADR: `docs/decisions/0047-fail-closed-sensitivity-guard.md` (PROPOSED).
> Branch: `gate/e-sensitivity-guard` off `master@34a6cbf` (Gate D merged, clean cut).
> Person-affecting posture: **NEUTRAL (fail-closed) — no human sign-off required** (§9).

---

## 1. Why — the gap (G6)

`resolution/review.py` guards merges against a **hardcoded denylist** that **fails OPEN** two ways.

**`review.py:22` (the denylist):**

```python
SENSITIVE_TOPICS = frozenset(
    {"sanction", "sanction.linked", "poi", "crime", "crime.fraud", "crime.terror", "wanted"}
)
```

`is_sensitive` (`review.py:27-33`) matches that 7-code set plus a `role.pep*` / `sanction*` prefix
rule. Against FtM 4.9.2 `registry.topic.RISKS` (28 codes — §2) this catches **10** of the 28 risk
codes and **MISSES 18** (verified — §2):

```
corp.disqual  crime.boss   crime.fin    crime.theft   crime.traffick  crime.war
debarment     export.control  export.control.linked  export.risk
invest.ban    invest.risk  mare.detained  mare.shadow  reg.action    reg.warn
role.oligarch  role.rca
```

A cluster whose only risk signal is one of those 18 (e.g. a war criminal `crime.war`, a crime boss
`crime.boss`, a relative-close-associate `role.rca`, a debarred supplier `debarment`, an
export-control target `export.control`, a regulator action `reg.action`) is **non-sensitive to the
guard → auto-merges with no human review.** That violates the CLAUDE.md catastrophic-merge invariant
("never auto-merge a sensitive entity").

**Fail-open (b) — the edge-less / no-topic entity.** `is_sensitive` reads `entity.get("topics",
quiet=True)` (`review.py:30`). A schema with no `topics` property (e.g. a `Sanction` object whose
risk lives on its *target*), or an entity whose risk is only expressible structurally (it sits next
to a sanctioned node but carries no risk topic of its own), returns `[]` → **not flagged.** Topic
presence is treated as the sole sensitivity signal; structural risk is invisible.

**The blast site.** `needs_review(cluster, by_id)` (`review.py:36`) is **pure** — it has NO neo4j
handle. It is called once, at `pipeline.py:357`. A sensitive verdict under `MERGE_GUARD_MODE="block"`
→ `record_merge(decision="pending_review")` + `_set_status(..., "pending_review")` (`pipeline.py:363-367`)
→ the ADR-0031 sign-off CLI. A non-sensitive (= guard-missed) verdict → auto-promote + write.

---

## 2. Verify-before-code (BLOCKING — see VERIFIED_API.md)

No implementation may begin until `VERIFIED_API.md` carries a **"Gate E — followthemoney risk
topics"** section recording, **verbatim against the INSTALLED FtM (not docs / not this spec)**:

- The pin: `followthemoney==4.9.2`, `followthemoney/types/topic.py`.
- The programmatic risk set: `from followthemoney.types import registry; registry.topic.RISKS`
  — a **`set[str]` of exactly 28 codes** (verified `type(...).__name__ == "set"`, `len == 28`). This
  is **FtM's OWN risk tag**, not a label heuristic and not our list. The 28 codes, sorted, **verbatim**:

  ```
  corp.disqual  crime  crime.boss  crime.fin  crime.fraud  crime.terror  crime.theft
  crime.traffick  crime.war  debarment  export.control  export.control.linked  export.risk
  invest.ban  invest.risk  mare.detained  mare.shadow  poi  reg.action  reg.warn
  role.oligarch  role.pep  role.rca  sanction  sanction.control  sanction.counter
  sanction.linked  wanted
  ```

- The full topic vocabulary: `registry.topic.names` — **73** names; `registry.topic.RISKS` is a
  subset of it (`RISKS <= set(registry.topic.names) == True`).
- The **off-ontology probe**: a code NOT in `registry.topic.names` (e.g. `"foo.bar"`,
  `"cti.apt"`, an enricher's vocab) → `in registry.topic.names == False`. **This is the inversion
  hinge:** a topic code unknown to the FtM vocabulary is treated as **sensitive (unknown ⇒
  sensitive)**, never auto-merged.
- The legacy-miss reconciliation (record the numbers): the `review.py:22` denylist + the
  `role.pep*`/`sanction*` prefix rule catches **10** of the 28; **18 are missed** (the list in §1).

A missing/paraphrased entry, a wrong module path, or a stale code count is a judge **DENY (E-VERIFY)**.

> The risk SET is **loaded programmatically from FtM at runtime**, never copied into config or code
> as a literal. The literal list above lives in VERIFIED_API.md and this spec only as the
> verification record / test oracle — the implementation reads `registry.topic.RISKS`.

---

## 3. Design — deny-by-default: topics-first → graph(k-hop) → Chow abstain

The guard inverts from "allow unless on a small denylist" to **"hold for review unless provably
benign."** A cluster is evaluated in three ordered stages; the FIRST stage that flags wins, and an
**unknown / inconclusive result at any stage routes to `PENDING_REVIEW`** (fail-closed).

### 3.1 New package `src/worldmonitor/guard/`

A new top-level package `src/worldmonitor/guard/` with `sensitivity.py`. It owns the sensitivity
decision; `resolution/review.py` **delegates** into it (it does not re-implement). Rationale: the
guard now needs a Neo4j handle (k-hop) and FtM-registry access — keeping it out of `resolution/`
avoids an import cycle (`resolution` → `graph` is already one-directional via the writer) and gives
the inversion its own test surface. `resolution/review.py` keeps its public names
(`needs_review`, `is_sensitive`) and forwards.

### 3.2 Stage 1 — topics-first (deny-by-default, unknown ⇒ sensitive) — PURE, no graph

For each cluster member, read `entity.get("topics", quiet=True)` into `topic_codes`. The member is
sensitive iff:

- `topic_codes & registry.topic.RISKS` is non-empty (FtM's own risk tag — catches all 28), **OR**
- any code in `topic_codes` is **off-ontology**: `code not in registry.topic.names` (unknown ⇒
  sensitive — an enricher/CTI/crypto vocab the FtM model has never seen is treated as risky).

This subsumes and replaces `SENSITIVE_TOPICS` and the `role.pep*`/`sanction*` prefix rule (both of
which become redundant — every code they matched is in `RISKS`). The legacy `SENSITIVE_TOPICS`
constant is **deleted** (DENY E-DENYLIST if it survives as a live sensitivity source — §11).

Stage 1 alone closes the **headline G6** (the 18 missed risk codes) and is **pure** (no Neo4j).

### 3.3 Stage 2 — k-hop graph sensitivity (Neo4j) — closes fail-open (b)

No graph sensitivity exists today. A cluster member that carries no risk topic of its own may still
be structurally adjacent to a risk node (an edge-less `Sanction` object pointing at it, an
intermediary between two sanctioned parties). Stage 2: for each member's durable id, MATCH it in the
graph and check whether a **risk-labelled node lies within `k` hops**.

- **Risk node detection in-graph.** ftmg encodes topics as **node labels** (confirmed:
  `graph/gds.py` `is_sanctioned` reads `"Sanction" in labels`; `generate_topic_labels`). A risk node
  is one carrying any label derived from a `registry.topic.RISKS` code. The label set is computed
  in-code from `RISKS` (capitalised the way ftmg labels topics — the builder records the exact
  label-casing in VERIFIED_API.md against `generate_topic_labels`) and matched in the Cypher.
- **`:Ghost` exclusion (Gate D / ADR 0046) — HARD INV.** A `:Ghost` endpoint (no anchor props,
  structurally inert, a never-ingested traversal target) **MUST NEVER count as a sensitivity or
  corroboration signal.** The traversal excludes ghosts: `AND NOT n:Ghost` on every matched node,
  **and does not bridge THROUGH a ghost** (the recommended choice: terminate at, never traverse
  through, a ghost — a ghost is not evidence of anything). DENY E-GHOST if a ghost neighbour flags or
  un-flags a cluster, or if a path bridges through one.
- **k is an int-validated in-code config constant, INLINED.** `execute_read` casts the query to
  `LiteralString` and external input must never be interpolated. The hop depth `k` comes from
  `settings.sensitivity_khop_depth` (pydantic, `int`, `ge=0`), is validated as an `int`, and is
  **f-string-inlined into the `[*1..k]` variable-length pattern** at query-build time — it is config,
  not external input, and is **NEVER** passed as a `$param` (Neo4j forbids a param in a var-length
  bound, and inlining a validated int keeps the `LiteralString` contract sound). The matched
  durable id IS passed as a `$param` (it is data). DENY E-CYPHER if `k` is interpolated from any
  non-config source or if a durable id is string-formatted into the query.
- `k = 0` disables Stage 2 (member node itself only — still covered by Stage 1's topic read; Stage 2
  becomes a no-op). This is the kill-switch if graph adjacency proves too broad in the field.

### 3.4 Stage 3 — Chow (1970) abstain band — park-vs-auto-merge on an ALREADY-FORMED cluster

A cluster that survives Stages 1-2 (no member risk topic, no risk neighbour, no off-ontology code) is
not provably sensitive — but a **low-confidence** cluster is not provably benign either. Apply a
**Chow reject-option band** (Chow 1970, "On optimum recognition error and reject tradeoff") over the
cluster's **already-computed** `ResolvedCluster.score` (the weakest-link match probability,
`merge.py:78`):

- If `score` falls in an **abstain band** `[abstain_low, abstain_high)` → route to `PENDING_REVIEW`.
- The band is bounded **strictly above** the merge axis: `abstain_high <= DEFAULT_MERGE_THRESHOLD`
  is **not** required (a cluster only exists because it already cleared 0.92), so the band lives in
  `[DEFAULT_MERGE_THRESHOLD, 1.0)`. The recommended default parks the **lowest-confidence merges**
  (e.g. `[0.92, 0.95)`) for review while a near-certain merge (`>= 0.95`) auto-promotes.

**The abstain band is the park-vs-auto-merge axis on a cluster that has ALREADY formed. It is NOT the
merge-vs-no-merge axis.** Stage 3:

- **MUST NOT** read, write, or shift `DEFAULT_MERGE_THRESHOLD = 0.92` (`merge.py:34`).
- **MUST NOT** touch any Splink weight / blocking rule / `cluster_and_merge` membership / `pick_anchor`
  precedence. The cluster is given; Stage 3 only decides park-vs-promote.

DENY **E-THRESHOLD** if `merge.py`, the Splink model, or clustering is modified.

### 3.5 Same sink — no new park path

A Stage-1/2/3 flag returns `(True, reason)` from `needs_review`, exactly as today, into the
**existing** `pipeline.py:363-367` `pending_review` → ADR-0031 sign-off sink. **No new sink, no new
status, no new table.** The guard can only move MORE clusters to the existing `pending_review` set.

---

## 4. The threading change — `needs_review` gets the Neo4j handle

`needs_review` is pure today (no graph handle); Stage 2 needs one. The pipeline already owns a
`Neo4jClient` (`resolve_pending(..., neo4j: Neo4jClient, ...)` `pipeline.py:79`, threaded to
`_resolve_batch` `pipeline.py:121`). **Thread that existing client into the single call site:**

```
# pipeline.py:357 (signature change — pass the pipeline's existing neo4j client)
flagged, reason = needs_review(cluster, by_id, neo4j=neo4j)
```

`needs_review(cluster, by_id, *, neo4j: Neo4jClient | None = None)` — `neo4j` keyword-only, defaulting
`None` so the pure unit tests (Stage 1 only) call it without a graph. When `neo4j is None`, Stage 2
is skipped (Stage 1 + Stage 3 still run — fail-closed on topics + abstain). The delegation:
`review.needs_review` → `guard.sensitivity.needs_review`. No other call site exists (verified:
`needs_review` appears only at `review.py:36` and `pipeline.py:357`).

---

## 5. THE #1 REGRESSION FENCE — the approved-group exemption (`pipeline.py:359-360`)

```python
# pipeline.py:359-360 (runs AFTER needs_review)
if flagged and any(members <= group for group in approved_groups):
    flagged = False  # exactly an already-approved merge — promote, never re-park
```

`_approved_groups` (`pipeline.py:153-179`) builds connected components of POSITIVE sign-off
judgements. The exemption un-flags a cluster whose members are a **subset** of a human-approved group.

**The fail-closed hole.** The inversion now flags clusters the old denylist missed (e.g. a `role.rca`
member). If such a cluster's members are a subset of an **OLD approved group recorded BEFORE that
topic was understood to be sensitive**, the exemption would **silently un-flag it → auto-merge** —
exactly the auto-merge fail-closed is meant to prevent. This is the **one path where fail-closed could
accidentally NOT park.**

**The spec REQUIRES a fix + a regression test.** The planner picks the mechanism; both of these are
acceptable and the chosen one must be recorded in ADR 0047:

- **(A) Reason-scoped exemption.** The exemption only un-flags a cluster flagged for a reason the
  approval **covered**. A cluster newly flagged for a sensitivity reason (Stage 1/2/3) the approval
  could not have considered is NOT exempted. (Requires the approval to carry, or the guard to derive,
  what was reviewed.)
- **(B) Sensitivity overrides the exemption (recommended — simplest, most conservative).** A
  **sensitivity** flag (Stage 1/2/3) is NOT exemptible by an approved group; only the
  **size** flag and the **anchor-conflict** flag (ADR 0040) remain exemptible. Rationale: a sign-off
  approving "these are the same entity" is orthogonal to "this entity is sanctioned/criminal" — the
  latter always deserves a fresh human look, which is the whole point of deny-by-default. This keeps
  the change inside the guard's return contract (the guard tags WHY it flagged; the exemption checks
  the tag) and never weakens the existing approve-to-promote path for non-sensitive merges.

**Required test (frozen into the suite):** a newly-broadened-sensitive cluster (flagged for a risk
code the legacy denylist missed, e.g. `role.rca`) whose members are a subset of a stale approved
group is **NOT auto-promoted** — it routes to `pending_review`. DENY **E-STALE-EXEMPT** if a
previously-parked-able cluster can now slip through a stale exemption, or if this test is absent.

---

## 6. Config (pydantic `BaseSettings`, env-driven — NO YAML)

New fields on `Settings` (`src/worldmonitor/settings.py`). The risk SET is **not** configured (it is
loaded from FtM); config is the k-hop / abstain knobs + the WM-070 extension surface.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `sensitivity_khop_depth` | `int` (`ge=0`) | `1` | Stage-2 traversal depth. `0` disables Stage 2 (kill-switch). Inlined as the validated `[*1..k]` bound. |
| `sensitivity_abstain_low` | `float` (`ge=0.0, le=1.0`) | `0.92` | Stage-3 band lower bound (inclusive). Defaults to `DEFAULT_MERGE_THRESHOLD` (no park) so the abstain band is OFF until tuned — see note. |
| `sensitivity_abstain_high` | `float` (`ge=0.0, le=1.0`) | `0.92` | Stage-3 band upper bound (exclusive). `low == high` ⇒ empty band ⇒ Stage 3 is a no-op (default). |
| `sensitivity_extra_topics` | `frozenset[str]` / CSV env | `()` | **WM-070 extension surface.** Additional codes treated as sensitive (a CTI/crypto enricher's vocab a human has classified as risky). UNION with `RISKS`; never SUBTRACTS. There is **no override that REMOVES** a `RISKS` code (deny-by-default cannot be configured open). |

A field-validator enforces `sensitivity_abstain_low <= sensitivity_abstain_high`. **Defaults ship the
abstain band OFF (`low == high`)** so slice-2 is a no-op until a human tunes it — the headline G6 fix
(Stage 1) and the structural fix (Stage 2 at `k=1`) carry the gate; Stage 3 is a knob the operator
opts into. DENY **E-CONFIG-OPEN** if any config field can REMOVE a `RISKS` code from the sensitive set.

---

## 7. Acceptance criteria — APPROVE / DENY

### APPROVE requires ALL of:

- **A1** — Every one of the 28 `registry.topic.RISKS` codes, present on any cluster member, routes
  the cluster to review; the cluster **never auto-merges** (parametrised over all 28 — §8 T2).
- **A2** — A risk code the **legacy denylist missed** (one of the 18, e.g. `crime.war` / `role.rca`)
  routes to review (pre-fix: AUTO-MERGES — the failing test — §8 T1).
- **A3** — An **edge-less / no-topic sanctioned entity** (risk only on its target / structural) is
  flagged (closes fail-open (b) — §8 T3 / T5).
- **A4** — An **off-ontology** topic code (`not in registry.topic.names`) ⇒ the cluster is
  abstained/sensitive (unknown ⇒ sensitive — §8 T6).
- **A5** — A member **within `k` hops of a risk node** is flagged; a **`:Ghost` neighbour does NOT
  flag** it (Stage 2 + ghost exclusion — §8 T5).
- **A6** — The **stale approved-group exemption** does NOT auto-promote a newly-broadened-sensitive
  cluster (§5 — §8 T4).
- **A7** — The risk SET is loaded **programmatically** from `registry.topic.RISKS`; the legacy
  `SENSITIVE_TOPICS` literal is gone as a live source of truth.
- **A8** — Every FROZEN test (§10) passes byte-for-byte unchanged.

### DENY (any one) — judge weights the adversarial target heaviest:

- **E-VERIFY** — VERIFIED_API.md lacks the verbatim `registry.topic.RISKS` record / 28-code count /
  off-ontology probe / module path (§2).
- **E-DENYLIST** — a hardcoded denylist remains the **SOLE source of truth** for sensitivity (the
  inversion didn't happen).
- **E-GRAPHONLY** — graph-only logic can pass an **edge-less risk entity** (Stage 2 without Stage 1,
  so a topic-bearing member with no graph node escapes).
- **E-THRESHOLD** — `DEFAULT_MERGE_THRESHOLD` / any Splink weight / clustering / `pick_anchor` is
  touched, **or anything previously parked now auto-merges**.
- **E-STALE-EXEMPT** — a stale approved-group exemption un-flags a newly-broadened-sensitive cluster
  (§5), or its regression test is absent.
- **E-GHOST** — a `:Ghost` counts as a sensitivity/corroboration signal, or a path bridges through one.
- **E-CYPHER** — `k` is interpolated from non-config input, or a durable id is string-formatted into
  the query (must be a `$param`).
- **E-CONFIG-OPEN** — config can REMOVE a `RISKS` code from the sensitive set.
- **E-FROZEN** — a FROZEN test (§10) is removed, skipped, xfail'd, or loosened.

### Adversarial target (judge weights heaviest)

> **(a)** A **risk topic added upstream after the denylist was written** (modelled here by any of the
> 18 missed codes, or an off-ontology code) — it must route to review. **(b)** An **edge-less
> sanctioned entity** (risk on its target, no topic of its own, sitting one hop from a risk node) —
> it must be flagged via Stage 2, while a `:Ghost` neighbour must NOT flag it.

---

## 8. Failing-test-first + the test plan — `tests/test_sensitivity_guard.py` (NEW)

A NON-VACUOUS failing test exists before any fix. **T1 fails on `master@34a6cbf` (auto-merges) and
passes post-fix.** Mostly unit (Stages 1+3); Stage 2 is integration (Neo4j, Docker available).

| # | Test | Kind | Asserts |
|---|---|---|---|
| **T1** | A risk code OMITTED from the legacy denylist (e.g. `crime.war` / `role.rca`) on a 2-member non-sensitive-otherwise cluster routes to review | unit | `needs_review(...)[0] is True`. **Pre-fix: AUTO-MERGES → FAILS.** The failing-test-first oracle. |
| **T2** | **For EVERY one of the 28 `registry.topic.RISKS` codes**, the cluster never auto-merges | unit (parametrised over `registry.topic.RISKS`) | `needs_review(...)[0] is True` for all 28 |
| **T3** | An edge-less / no-topic sanctioned entity is flagged | unit | the 2b fail-open closes — flagged (via topic on its asserting object, or Stage-2 in T5) |
| **T4** | A newly-broadened-sensitive cluster (e.g. `role.rca`) ⊆ a STALE approved group is NOT auto-promoted | integration (pipeline + judgements) | routes to `pending_review`; status set; merge_alerts/promote NOT taken (§5) |
| **T5** | Stage 2: a member within `k` hops of a risk node is flagged; a `:Ghost` neighbour does NOT flag it | integration (Neo4j) | risk-neighbour ⇒ flagged; ghost-neighbour ⇒ not flagged; no bridging through a ghost |
| **T6** | An off-ontology topic code (`not in registry.topic.names`) ⇒ sensitive/abstain | unit | unknown ⇒ flagged |
| **T7** *(optional, see §13)* | G5 boundary: a 10-member cluster auto-merges; an 11-member cluster parks | unit | only if G5 folded in |

Each slice's tests must be **CI-green at that slice's merge** (slice-1 ships T1/T2/T3(topic)/T4/T6;
slice-2 ships T5 + the Stage-3 band test).

---

## 9. Person-affecting assessment — NEUTRAL (no sign-off)

Deny-by-default can only move **MORE** clusters to `pending_review`; it **auto-promotes NOTHING** the
old guard parked, and **strengthens** the human-sign-off net. The CLAUDE.md self-improvement rule
requires sign-off only for a change that could **auto-affect a real person without review** — this
gate does the opposite. **No sign-off required; autonomously buildable + mergeable.** The one
person-relevant surface (the approved-group-exemption fix, §5) makes the net STRICTER (un-flags
fewer clusters), never looser. **MUST NOT:** change `DEFAULT_MERGE_THRESHOLD` / Splink, or auto-merge
anything the old denylist parked.

---

## 10. FROZEN (keep-green, unchanged) tests

Gate E is additive to the guard's flag-set; these must pass byte-for-byte. A removed assert, an added
skip/xfail, or a loosened tolerance is a judge DENY (**E-FROZEN**):

- `tests/unit/test_resolution.py` — `needs_review` happy-path (singleton/clean merge not flagged).
- `tests/unit/test_resolution_anchor_conflict.py` — the ADR-0040 anchor-conflict park. **Asserts MUST
  NOT be weakened.** ONLY the now-stale comment at lines 26-28 ("The catastrophic-merge guard fires
  today only on sensitivity or cluster size > 10") is updated to note Gate E broadened the
  sensitivity axis (the fixtures stay non-sensitive — see below). This is a comment edit only.
- `tests/unit/test_resolution_merge_incompat.py`, `test_resolution_distinguishing_evidence.py`,
  `test_resolution_multiscript.py`, `test_resolution_negative_judgement.py`, `test_canonical.py`,
  `test_anchors.py`.
- `tests/integration/test_resolution_pipeline.py`, `test_resolution_batching.py`,
  `test_b6_resolve_incompat.py`, `test_b6_signoff_poison.py`, `test_b1_crash_recovery.py`,
  `test_b1_signoff_idempotency.py`, `test_signoff.py`.
- The size-park (`MAX_AUTO_MERGE_SIZE`) behaviour and the anchor-conflict park stay green.

**The no-happy-path-regression proof.** `tests/fixtures/opensanctions_entity.json` carries
`topics: ["sanction"]` — caught by BOTH the old denylist AND `registry.topic.RISKS` — so its
existing behaviour is unchanged: it proves the inversion did not regress the happy path. The
anchor-conflict fixtures (`test_resolution_anchor_conflict.py`) are deliberately **non-sensitive**
(no `topics`); since Gate E's Stage 1 still keys on `topics`, they stay non-sensitive and the
anchor-conflict park (ADR 0040) remains the load-bearing flag — only the stale prose comment changes.

---

## 11. Out of scope — hard stops

- `DEFAULT_MERGE_THRESHOLD` / Splink weights / `merge.py` clustering / `pick_anchor` precedence
  (**E-THRESHOLD**).
- The anchor-conflict park itself (ADR 0040) — frozen, comment-only edit (§10).
- Auto-promoting anything — the gate only ADDS to `pending_review`.
- A new sink / status / table — reuse the ADR-0031 `pending_review` → sign-off path (§3.5).
- Bridging-through-`:Ghost` or any ghost-as-evidence path (**E-GHOST**).
- Postgres / Alembic migration — none expected (the guard is read-only against Neo4j + Postgres
  judgement reads). If the builder concludes a migration IS needed, **STOP and flag the human**
  (scope signal — **E-MIGRATION**). `tests/integration/test_migrations.py` stays green.
- A live API/MCP surface exposing the sensitivity decision (capability-only this gate).

**G5 adjacency:** the `MAX_AUTO_MERGE_SIZE = 10` boundary (audit G5) is the same file + same ADR
lineage (0020). The gate MAY fold it in (§13) — making it config + adding the 10/11 boundary test —
or leave it to **WM-074**. The builder/planner decides and records the choice; it is NOT required.

---

## 12. Slice breakdown

**Two slices.** Each is individually CI-green + mergeable; land slice-1 first.

### slice-1 — topics-first deny-by-default (PURE, no graph) — `human_fork=false`

Closes the headline G6. Pure `guard/sensitivity.py` Stage 1 (programmatic `RISKS` + unknown ⇒
sensitive + the legacy `SENSITIVE_TOPICS` deletion), `review.py` delegation, and the
**approved-group-exemption fix (§5)** with its regression test. No Neo4j, no signature change beyond
the `neo4j=None` keyword default (Stage 2 is a stub returning "not-sensitive" until slice-2). Ships
T1, T2, T3 (topic-borne), T4, T6.

- Files: `src/worldmonitor/guard/__init__.py`, `src/worldmonitor/guard/sensitivity.py`,
  `resolution/review.py`, `resolution/pipeline.py` (the exemption fix + the `neo4j=` plumbing),
  `tests/test_sensitivity_guard.py` (+ `tests/unit/...`), `tests/unit/test_resolution_anchor_conflict.py`
  (comment-only), `VERIFIED_API.md`, the spec, ADR 0047, `docs/decisions/0020-*.md` (note-invert),
  `docs/GATE_LEDGER.md` (G6 row).

### slice-2 — k-hop graph sensitivity + Chow abstain band + config — `human_fork=false`

Stage 2 (Neo4j threading at `pipeline.py:357`, the `[*1..k]` inlined-validated-int query, `:Ghost`
exclusion) + Stage 3 (Chow abstain band over `ResolvedCluster.score`) + the `settings.py` fields.
Ships T5 + the Stage-3 band test.

- Files: `guard/sensitivity.py` (Stage 2/3 fill-in), `resolution/pipeline.py` (the call-site neo4j
  thread — if not already done in slice-1), `settings.py` (config fields), and **ONLY IF** a reusable
  k-hop helper lands in `graph/`: `graph/gds.py` or `graph/neo4j_client.py` (scope it in
  `.claude/gate.scope`).

**Why split:** slice-1 is pure, closes the headline gap, and is the highest-value, lowest-risk
change — it should not wait on a Neo4j-integration slice. Stage 2/3 add a graph dependency, a
signature thread, and tunable bands that benefit from landing behind an OFF-by-default config. If the
builder finds the `neo4j=` thread trivially clean, slices MAY collapse to one — but the default is two.

---

## 13. G5 fold-in decision (recommended: defer to WM-074)

**Recommendation: leave G5 to WM-074.** Folding the `MAX_AUTO_MERGE_SIZE` boundary in adds a config
field + a 10/11 test in the SAME file, which is low-cost, but it dilutes the gate's single message
(fail-closed *sensitivity*), and the size guard does NOT fail open the way the denylist does (it is
conservative-by-default already — it just lacks a boundary test). If the planner folds it in anyway,
it is slice-1-adjacent (pure), ships T7, and must make `MAX_AUTO_MERGE_SIZE` a config field
(`sensitivity_max_auto_merge_size`, default `10`) without changing the default behaviour. Record the
choice in ADR 0047.

---

## 14. Locked invariants (must hold across the gate)

- **G1 provenance on every node AND edge** — PRESERVED. The guard is READ-ONLY against the graph; it
  writes no node/edge. DENY if any prov_* is dropped.
- **Append-only / no un-merge** — PRESERVED. The guard moves clusters to `pending_review`; it never
  mutates or un-merges an existing canonical.
- **Canonical-canonical only via the guard** — PRESERVED + STRENGTHENED. The guard is the merge gate;
  this gate makes it stricter and never auto-fuses two canonicals.
- **Never auto-promote previously-parked (HARD INV)** — nothing the old denylist or size/anchor guard
  parked may now auto-merge. DENY E-THRESHOLD / E-STALE-EXEMPT.
- **Approved-group-exemption fence (HARD INV)** — a stale approval may not un-flag a
  newly-broadened-sensitive cluster (§5).
- **`:Ghost` exclusion (HARD INV)** — a ghost never counts as sensitivity/corroboration and is never
  bridged through (§3.3).
- **Verify-before-code (HARD INV)** — VERIFIED_API.md records the FtM `registry.topic.RISKS` API
  verbatim before code (§2).
- **NO `DEFAULT_MERGE_THRESHOLD` / Splink / clustering / `pick_anchor` change (HARD INV)** — the
  abstain band is a distinct axis on an already-formed cluster (§3.4).

---

## 15. slice-3 — pre-merge hardening (post-review of slice-2)

> **Hardening slice**, NOT a new gate. Branch `gate/e-sensitivity-guard-2` (PR #93). slice-1 + slice-2
> are built; an adversarial verification of slice-2 (`c26570d`) returned **APPROVE_WITH_NITS** (CI
> green, no DENY) and surfaced findings to fix BEFORE the human merges. slice-3 does NOT relitigate
> the gate; it closes a masking fail-open in the slice-2 exemption fence and reconciles two doc/code
> mismatches. **Person-affecting posture: NEUTRAL (fail-closed)** — slice-3 is **monotonically
> STRICTER** (more clusters become non-exemptible ⇒ MORE parks); it auto-promotes nothing the slice-2
> fence parked. **No human sign-off** (§9 reasoning holds, strengthened).

### 15.1 Finding B (MEDIUM) — short-circuit masking fail-open (the core fix)

`needs_review` returns only the **FIRST** flag's reason (order: `size>10` → Stage-1 topic →
anchor-conflict → Stage-2 k-hop → Stage-3 Chow). The slice-2 fence (`pipeline.py:379-384`) derives
non-exemptibility from **(1)** `is_newly_broadened_sensitive` over the members (TOPIC-only,
member-derived — correctly NOT masked) and **(2)** `is_nonexemptible_reason(reason)` — a **substring
match of the SINGLE returned reason**. So when an **EXEMPTIBLE** flag fires FIRST (`size>10`,
anchor-conflict, or a **legacy-caught** topic such as `sanction`), a co-occurring **NON-exemptible**
Stage-2 k-hop or Stage-3 Chow signal is **never evaluated** → a cluster ⊆ a **stale** approved group
is silently un-flagged → **auto-promoted despite real k-hop risk-adjacency / marginal Chow
confidence**. This contradicts the fence's own comment at `pipeline.py:374-378`. It is **NOT a
regression** (slice-1 had no k-hop/Chow) and was not a slice-2 DENY, but it **under-realizes** the
fail-closed contract of §5/§14.

### 15.2 The fix — a STRUCTURED non-exemptibility probe (replaces the reason-string coupling)

Replace `is_nonexemptible_reason(reason)` with, in `guard/sensitivity.py`:

```
has_nonexemptible_sensitivity(cluster, by_id, *, neo4j=None) -> bool
```

returning `True` iff **ANY** of the following, each evaluated **INDEPENDENTLY** of `needs_review`'s
first-flag short-circuit:

- any member `is_newly_broadened_sensitive` (newly-broadened TOPIC — Stage 1, member-derived), **OR**
- `neo4j is not None and settings.sensitivity_khop_depth > 0` and any member
  `_risk_within_khop(neo4j, member_id, settings.sensitivity_khop_depth)` (Stage-2 graph proximity), **OR**
- `settings.sensitivity_abstain_low <= cluster.score < settings.sensitivity_abstain_high`
  (Stage-3 Chow band).

`pipeline.py` computes the fence from the probe, **not** the reason. Recommended (perf-neutral)
wiring — evaluate the probe **lazily, only on the exemption path**, so the common (non-exempt) case
adds **no** extra graph read:

```python
exempt = flagged and any(members <= group for group in approved_groups)
if exempt and not has_nonexemptible_sensitivity(cluster, by_id, neo4j=neo4j):
    flagged = False  # an exemptible (size / anchor-conflict / legacy-caught-topic) approved merge — promote
```

The human-readable `reason` returned by `needs_review` **STAYS** (it remains the audit / `record_merge`
reason). Correct the `pipeline.py:374-378` comment to describe the structured probe truthfully (it is
no longer "recognised off the guard's reason string"). **REMOVE** `is_nonexemptible_reason` and the
now-unused `_KHOP_REASON_MARKER` / `_ABSTAIN_REASON_MARKER` constants — grep-confirmed referenced only
in `guard/sensitivity.py`, `resolution/pipeline.py`, and the two **NON-frozen** slice-2 test files
(`tests/unit/test_exemption_fence.py`, `tests/integration/test_sensitivity_guard_khop.py`) which
slice-3 adapts; **no FROZEN test references them**. The Stage-2/Stage-3 reason f-strings in
`needs_review` keep their human-readable text **inlined** (they no longer reference the removed
constants).

### 15.3 Finding F (NIT) — classify structurally, not by substring

`is_nonexemptible_reason` substring-matched long English markers inside a **free-text reason that also
embeds data-bearing fields** (`member_id`, anchor VALUES — hostile data per CLAUDE.md): brittle and a
(theoretical) confused-deputy surface. The structured probe (15.2) **removes the coupling entirely** —
non-exemptibility is computed from the cluster + members + graph, **never** from a string.

### 15.4 Preserved FROZEN behaviour — both T4 cases stay green

The probe is constructed to preserve `tests/integration/test_sensitivity_guard.py` T4 byte-for-byte:

- **role.rca** (newly-broadened-topic) ⊆ stale approval → `is_newly_broadened_sensitive` True →
  probe **True** → re-parks (the T4 oracle). ✅
- **sanction** (legacy-caught-topic) ⊆ stale approval → `is_newly_broadened_sensitive` False; no risk
  node seeded ⇒ k-hop False; band OFF (`low==high`) ⇒ Chow False → probe **False** → auto-promotes
  (the T4 discriminator). ✅

### 15.5 ALSO — (C)/(D)/(E)

**(C) — VERIFIED_API.md E-VERIFY completeness (builder verifies against installed FtM).** Add to the
"Gate E" section, verbatim (confirmed against the installed `followthemoney==4.9.2`,
`.venv`): `registry.topic.names` is a **`dict`, `len == 73`**; `set(registry.topic.RISKS) <=
set(registry.topic.names) == True`; source path **`followthemoney/types/topic.py`**.

**(D) — WM-070 (`sensitivity_extra_topics`) is DEFERRED (reconcile ADR/spec to shipped code).** The
field is **NOT** implemented in `settings.py` (shipped fields: `sensitivity_khop_depth`,
`sensitivity_abstain_low`, `sensitivity_abstain_high` only). The §6 table row for
`sensitivity_extra_topics` is hereby marked **DEFERRED — NOT SHIPPED**; ADR 0047 Decision 6 is
reconciled likewise. **Do NOT implement `extra_topics` this slice.** Consequence: the deny-by-default
set is **exactly** `registry.topic.RISKS` + unknown⇒sensitive with **no config UNION surface at all**,
so **E-CONFIG-OPEN is trivially held** (no config field touches the sensitive set).

**(E) — `sensitivity_khop_depth` upper bound.** Add `le=4` to the field (keep `default=1`, `ge=0`
unchanged; `0` stays the kill-switch). One-line rationale: a var-length `[*1..k]` traversal is
exponential in `k`; a conservative ceiling stops a misconfiguration from launching an unbounded graph
scan in the resolve hot path. Note the operational ceiling in ADR 0047 §6.

### 15.6 New / changed invariants (slice-3)

- **STRUCTURED NON-EXEMPTIBILITY / NO-MASKING (NEW HARD INV).** A cluster that is **simultaneously**
  `[size>10 OR legacy-caught-topic OR anchor-conflict]` **AND** `[k-hop-adjacent to a non-ghost risk
  node OR Chow-in-band OR newly-broadened-topic]` **AND** ⊆ a **stale** approved group **MUST RE-PARK,
  never auto-promote.** Non-exemptibility is computed by `has_nonexemptible_sensitivity(cluster,
  by_id, neo4j)` — **independent of `needs_review`'s first-flag short-circuit and of the returned
  reason string.** The fence **MUST NOT** derive non-exemptibility by substring-matching a free-text
  reason. DENY **E-MASK** (a facet of E-STALE-EXEMPT).
- **MONOTONIC-STRICTER (held).** slice-3 only makes MORE clusters non-exemptible (more parks); it
  auto-promotes nothing the slice-2 fence parked. Person-NEUTRAL / fail-closed; no sign-off.
- All §14 invariants UNCHANGED (G1 prov on node+edge, append-only / no un-merge,
  canonical-canonical only via the guard, `:Ghost` exclusion, **no** `DEFAULT_MERGE_THRESHOLD` /
  Splink / `cluster_and_merge` / `pick_anchor` change, no migration).

### 15.7 Failing-first requirement + APPROVE / DENY deltas

A **non-vacuous failing-first** test is REQUIRED before the fix (§16): an integration test where a
cluster with an **exemptible-first** flag (a legacy-caught `sanction` member and/or `size>10`) ALSO
carries a **k-hop adjacency** to a seeded non-ghost risk node, ⊆ a **stale** approval → on slice-2
code it **AUTO-PROMOTES** (the masking fail-open) → the test **FAILS**; after
`has_nonexemptible_sensitivity` it **RE-PARKS** → PASSES.

**APPROVE delta (adds to §7):**
- **A9** — the masking case re-parks (§16 `T-MASK-khop` integration + `T-MASK-chow` unit).
- **A10** — non-exemptibility is **structural**: `is_nonexemptible_reason` + the marker constants are
  gone; no substring-of-reason classifier remains in the fence path.

**DENY delta (adds to §7):**
- **E-MASK** (facet of E-STALE-EXEMPT) — an exemptible-first flag masks a co-occurring
  k-hop/Chow/newly-broadened signal so a cluster ⊆ a stale approval auto-promotes; **or** the
  structured probe is absent; **or** the fence still classifies by reason-substring; **or** the
  masking failing-first test is absent.
- **E-FROZEN** unchanged — both slice-1 T4 cases (integration) stay byte-for-byte green.

### 15.8 slice-3 file list + Definition of Done

- **Code (builder):** `src/worldmonitor/guard/sensitivity.py` (add `has_nonexemptible_sensitivity`;
  delete `is_nonexemptible_reason` + the two marker constants; inline the Stage-2/3 reason text),
  `src/worldmonitor/resolution/pipeline.py` (probe-based fence + corrected `:374-378` comment + import
  swap), `src/worldmonitor/settings.py` (`sensitivity_khop_depth` gains `le=4`).
- **Docs:** `VERIFIED_API.md` (C), `docs/decisions/0047-fail-closed-sensitivity-guard.md` (Decision 5
  refinement note, Decision 6 WM-070 DEFERRED + khop cap), this spec.
- **Tests (test-author):** `tests/unit/test_exemption_fence.py` (REWRITE to the structured probe),
  `tests/integration/test_sensitivity_guard_khop.py` (ADAPT `test_t5e` to the structured probe),
  `tests/integration/test_exemption_fence_masking.py` (**NEW** — the masking failing-first).
- **DoD:** the masking failing-first test is **RED** on slice-2 code and **GREEN** after the fix; both
  T4 cases and every FROZEN test (§10) stay green; CI (`quality` + `security`) green; no DENY facet
  (incl. E-MASK) open.

---

## 16. slice-3 test plan (for the test-author)

| # | Test | File | Kind | Asserts |
|---|---|---|---|---|
| **T-MASK-khop** | The **failing-first** masking oracle. A cluster with an **exemptible-first** flag (a legacy-caught `sanction` member; size>10 is an equivalent parametrization) AND a member **one hop from a seeded non-ghost `:Sanction` node** AND ⊆ a **stale** approved group is driven through `resolve_pending(guard_mode="block")` | `tests/integration/test_exemption_fence_masking.py` (**NEW**) | integration (Neo4j + Postgres, CI-only) | `stats.review == 1`, `stats.promoted == 0`, nothing written to the graph, both queue rows → `pending_review`. **PRE-FIX (slice-2): the `sanction`/size flag short-circuits `needs_review` with an exemptible reason, `is_nonexemptible_reason` misses the k-hop signal, the stale exemption un-flags → AUTO-PROMOTES → FAILS.** POST-FIX: `has_nonexemptible_sensitivity` evaluates the k-hop independently → re-parks. |
| **T-MASK-chow** | The score-band masking, cheap. A **size>10** cluster (topic-clean, `neo4j=None`) scoring inside a configured Chow band `[0.90, 0.95)`: `needs_review(...)` returns the **SIZE reason** (proving the masking — the old reason-string path would see only the size reason); `has_nonexemptible_sensitivity(cluster, by_id)` returns **True** | `tests/unit/test_exemption_fence.py` | unit | `needs_review[1]` is the size reason; `has_nonexemptible_sensitivity(...) is True`. Failing-first by construction (the function does not exist on slice-2). |
| **Fence contract — "no wider"** | `has_nonexemptible_sensitivity` is **False** for every exemptible-only cluster: a pure size>10 (band OFF, `neo4j=None`), an anchor-conflict, a **legacy-caught `sanction`** (band OFF, `neo4j=None`), and a clean merge | `tests/unit/test_exemption_fence.py` (REWRITE) | unit | all `is False` — preserves the frozen approve→promote path for a knowingly-approved legacy-caught / size / anchor merge. |
| **Fence contract — "no narrower"** | `has_nonexemptible_sensitivity` is **True** for a **newly-broadened-topic** (`role.rca`) member, and **True** for a Chow-in-band cluster (band `[0.90,0.95)`, score `0.92`) | `tests/unit/test_exemption_fence.py` (REWRITE) | unit | both `is True`. |
| **T5e (adapted)** | `has_nonexemptible_sensitivity(merge, by_id, neo4j=clean_graph)` is **True** for the T5a risk-adjacent cluster (`near-1` one hop from a `:Sanction` node) and **False** when no risk node is seeded | `tests/integration/test_sensitivity_guard_khop.py` (ADAPT) | integration (Neo4j) | replaces the removed `is_nonexemptible_reason` coupling-guard with the structured probe's k-hop branch. |

**Notes for the test-author:**

- **k-hop needs Neo4j ⇒ integration (CI-only).** The score-band masking (`T-MASK-chow`) and both
  fence-contract directions are **pure unit** (no graph). `T-MASK-khop` is the end-to-end re-park
  proof and is the load-bearing failing-first.
- **`T-MASK-khop` construction (sanction variant, simplest):** seed a prior-batch
  `(:Entity:Person:Sanction {id:"risk-1"})-[:LINKED]->(:Entity:Person {id:"p2"})` (mirrors T5a);
  record a **stale** positive judgement `{p1, p2}`; re-ingest `p1` with `topics:["sanction"]`
  (legacy-caught — Stage 1 fires FIRST with an exemptible reason; `is_newly_broadened_sensitive(p1)`
  is False) and a topic-clean `p2` (graph-resolvable to `risk-1`). The **only** non-exemptible signal
  is `p2`'s k-hop adjacency; without the masking fix the `sanction` reason hides it. The size>10
  variant (11 topic-clean members, one graph-resolvable + adjacent, ⊆ a stale approval) is an
  equivalent parametrization.
- **Do NOT touch the FROZEN `tests/integration/test_sensitivity_guard.py`** (slice-1 T4). The new
  masking test lives in its own file so the frozen T4 stays byte-for-byte.
- **No private name is hand-referenced as a marker constant** — the adapted tests exercise the
  structured probe directly, so the brittle string coupling cannot regress silently.
