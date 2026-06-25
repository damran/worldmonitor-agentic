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
