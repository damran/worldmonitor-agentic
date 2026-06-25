# Gate B-front — Anchor-Preferred Stable IDs + Adopt/Merge/Split

> BUILD gate. Front half of Gate B / ADR 0019. **ADR:** `docs/decisions/0044-anchor-preferred-stable-ids.md`
> (PROPOSED). Branch `gate/b-front-stable-ids` off `master@9f42852` (cut, clean).
> Extends ADR 0036 + 0039; depends on ADR 0037; preserves ADR 0040 + 0031 + 0025 + 0042.

This spec converts the orient Situation Report into a buildable gate. It is **spec only** — no code.
A non-vacuous **failing-test-first** suite is required (`tests/test_stable_id.py`).

---

## 1. Why (the problem, verified)

`_canonical_id` (`resolution/merge.py:45-55`) returns `wmc-<sha256(sorted(member_ids))[:40]>` for a merge.
Member ids are **fresh per re-ingest** — connectors mint per-collect ids, and the ER queue dedups only on
`(source_record, entity_id)` (`db/models.py:46`). So re-ingesting the **same real entity** yields a
**different** `wmc-` id, a different graph node, and id churn that breaks every downstream reference and
alias. `wmc-<hash>` is a **crash-retry idempotency fingerprint** (ADR 0036's real guarantee), **not** durable
identity — ADR 0036 §Consequences explicitly deferred re-ingest stability to "Gate B."

**This gate separates the two concepts:** `wmc-` stays *strictly* an idempotency fingerprint (the fallback
id of an *unanchored* merge); durable identity becomes **anchor-preferred + a ledger + `canonical_alias`**.

### Current surface (file:line — verified)
- `_canonical_id` producers/consumers: `merge.py:45,203-204,224,258-259`; `ResolvedCluster.canonical_id:62`.
- `graph/writer.py:122/131/135/144` — native `{id}` MERGE (single-tenant, ADR 0042).
- `resolution/audit.py:23/46` — `MergeAudit/MergeAlert.canonical_id`.
- `db/models.py:70/123/190` — `canonical_id String(255)` on `MergeAudit`/`MergeAlert`/`SignOff`.
- `resolution/signoff.py:243/283/326` — sign-off re-merge under the audit's `canonical_id`.
- `resolution/referents.py:43` — `build_referent_map` maps member → canonical.
- `resolution/pipeline.py:292-294,341,367,377,407` — `cluster_and_merge` call + promote/park + referents.
- Anchors: `ontology/anchors.py:31` `CANONICAL_ID_FIELDS=("wikidata_id","geonames_id","lei","opencorporates_id")`,
  stored as `wm_anchor_*` context keys; `get_anchors` **omits** a conflicting field (ADR 0040 guard);
  Neo4j uniqueness on those fields (`graph/constraints.py:24-29`).
- Adopt/merge/split hooks land **after** `cluster_and_merge` returns, **before** `pipeline._resolve_batch`
  writes (between line 294 and the promote loop / line 410 write).

---

## 2. Verify-before-code gate (BLOCKING — judge DENY if missing or paraphrased)

**No implementation may begin until `VERIFIED_API.md` exists at repo root** and records — VERBATIM, against
the **installed nomenklatura 4.10.0 source** (not the brief, not docs) — every bound signature. This mirrors
Gate A's Splink requirement. The orient brief described the API at a top-level layout (`identifier.py`,
`resolver.py`); the **installed** package is namespaced under `nomenklatura/resolver/`. The builder must
record the REAL paths and signatures. Verified surface to confirm verbatim:

- `nomenklatura/__init__.py` — `from nomenklatura.resolver import Linker, Resolver`;
  `from nomenklatura.judgement import Judgement`. (`Identifier` is **not** a top-level export — it lives at
  `nomenklatura/resolver/identifier.py`.)
- `nomenklatura/resolver/identifier.py`:
  - `Identifier.PREFIX = "NK-"`; `__slots__ = ("id","canonical","weight")`.
  - `__init__`: `weight=1`; `weight=2` if `id.startswith("NK-")`; `weight=3` if `is_qid(id)`
    (`from rigour.ids.wikidata import is_qid`); `self.canonical = self.weight > 1`.
  - `__lt__`: orders by `(self.weight, self.id)`.
  - `make(cls, value: Optional[str] = None) -> "Identifier"` → `f"{cls.PREFIX}{value or shortuuid.uuid()}"`.
- `nomenklatura/resolver/resolver.py`:
  - `decide(self, left_id, right_id, judgement, user: Optional[str]=None, score: Optional[float]=None) -> Identifier`.
  - `get_canonical(self, entity_id: str) -> str` → `max(self.connected(node))`; returns `node.id` if the max
    is **not** canonical (i.e. an unjudged id is its own canonical).
  - `get_referents(self, canonical_id: str, canonicals: bool = True) -> Set[str]`.
  - `get_judgement(self, entity_id, other_id) -> Judgement` (component-aware; the ADR-0037 dependency).
  - `remove(self, node_id) -> None`; `explode(self, node_id) -> Set[str]` (dissolves the cluster).
  - `begin(self, load_edges=True)`, `commit()`, `rollback()`, `make_default(cls, engine=None)`.
- `nomenklatura/judgement.py` — `Judgement(Enum)`: `POSITIVE="positive"`, `NEGATIVE="negative"`,
  `UNSURE="unsure"`, `NO_JUDGEMENT="no_judgement"`.
- FtM (`followthemoney`) — `registrationNumber`, `taxNumber`, `leiCode`, `wikidataId` exist on `Company`,
  `LegalEntity`, `Person`, `Organization` and are all FtM type **`identifier`** (verified this gate).
  `rigour.ids.wikidata.is_qid("Q42") is True`; `is_qid(<LEI>) is False`.

**The KEY verified fact that drives the design decision:** nomenklatura already does anchor-preferred
canonical selection but **QID-only** — `get_canonical` returns `max(connected)` and only a QID has
`weight=3`; LEI/regNo/taxNo are weight-1 raw ids it would never deterministically prefer. The durable
precedence is richer than QID-only, AND the resolver discards its mapping on teardown (ADR 0028). So the
durable id MUST be derived OUTSIDE the resolver. **DENY if `VERIFIED_API.md` is missing, paraphrased, or
binds the wrong module path** (spec §10 D2).

---

## 3. The durable-id derivation decision (canonical.py vs extending the resolver)

**DECISION: derive OUTSIDE the resolver in a new `resolution/canonical.py`.** (ADR 0044 §1, alt-A rejected.)
The resolver decides *membership*; `canonical.py` decides the *durable id* — matching how `_canonical_id`
already lives outside the resolver. `canonical.py` exposes:
- `pick_anchor(members: Sequence[FtmEntity]) -> str | None` — the anchor-preferred durable id, honoring the
  precedence (§4) and the anchor-conflict guard (§5); `None` if no usable anchor exists.
- ledger read (`lookup_alias`/`resolve_durable`) + write (`record_canonical`/`record_alias`) helpers.
- `mint() -> str` → `wm-mint-<uuid>` for an unanchored cluster with no prior ledger entry.

`canonical.py` MUST stay a **pure, DB-thin** module (the ledger helpers take a `Session`; `pick_anchor` is
DB-free and unit-testable like `cluster_and_merge`).

---

## 4. Precedence + anchor reconciliation

**Precedence (ordered, first-hit-wins): QID > LEI > regNo > taxNo > minted `wm-mint-<uuid>`.**

Mapping (the durable precedence defines its OWN list — it does NOT reuse `CANONICAL_ID_FIELDS` verbatim,
which has the wrong storage keys, no regNo/taxNo, and no producer for lei/opencorporates):

| Tier | Source | Validity check | Durable id form (builder records final) |
|---|---|---|---|
| QID | FtM `wikidataId` (or `wm_anchor_wikidata_id` context) | `rigour.is_qid` | `qid:<Qxxx>` |
| LEI | FtM `leiCode` (or `wm_anchor_lei` context) | 20-char LEI shape | `lei:<…>` |
| regNo | FtM `registrationNumber` (ADR-0039 `identifier`-normalized) | non-empty | `regno:<…>` |
| taxNo | FtM `taxNumber` (ADR-0039 `identifier`-normalized) | non-empty | `taxno:<…>` |
| mint | — | — | `wm-mint-<uuid>` |

Reconciliation rules (orient flagged these — they are load-bearing):
- `CANONICAL_ID_FIELDS` and the Neo4j uniqueness constraints (`constraints.py`) are **unchanged**. The
  durable precedence is a **separate** ordered list (lands in `ontology/anchors.py` if it shares the anchor
  vocabulary; else in `canonical.py`). `geonames_id`/`opencorporates_id` are NOT in v0 durable precedence
  (no producer; GeoNames is a place anchor, not a legal-identity anchor) — documented so a later producer
  extends the precedence, not the constraints.
- regNo/taxNo are normalized via the FtM `identifier` type exactly as ADR 0039 does (`_distinguishing_ids`),
  so the same id stored as `taxNumber` on one record and `registrationNumber` on another reconciles
  (this is **cross-property** reconciliation — regNo↔taxNo read into both tiers; matching is **exact-string
  after `registry.identifier.clean`**, NOT fuzzy, so `"HRB 12345"` ≠ `"hrb12345"` and fall through to a
  mint rather than fuse — the conservative, person-safe choice consistent with ADR 0039).
- The durable id is anchor-kind-prefixed (above) to prevent cross-namespace collision (e.g. a regNo equal to
  a QID string). Final serialization is a builder record in ADR 0044.

---

## 5. The anchor-conflict guard (ADR 0040) is PRESERVED — never pick a durable id from a conflicting set

`pick_anchor` MUST consult `anchor_conflicts_across(members)` semantics. If the cluster's members carry
**two distinct values for the anchor at the chosen tier** (two QIDs, two LEIs, two regNos, …), the durable
id is **NOT** derived from that tier — `pick_anchor` falls through to the next non-conflicting tier, or to a
mint. It MUST NOT silently pick `[0]`. This is exactly the catastrophic-merge case ADR 0040 parks; deriving
a durable id is **never** a back-door fusion of two real-world identities. This is the gate's #1 person-
safety property (§7).

---

## 6. The ledger + `0006` migration + drift guard

New table `canonical_id_ledger` (final name/columns a builder record in ADR 0044):
- `canonical_id` (durable; anchor-preferred or minted) — indexed.
- `canonical_alias` (a superseded/prior id resolving to it) — one row per alias; **append-only**.
- anchor kind + value (or `mint`).
- `created_at`.

Migration **`0006_canonical_ledger.py`**, **`down_revision = "0005_er_gold_pair"`**. The model in
`db/models.py` and the `0006` head MUST agree **byte-for-byte** — `tests/integration/test_migrations.py`
(ADR 0030 drift guard) asserts `fresh(alembic head) == create_all(models.py)` and `alembic check` clean.
**Write the `models.py` edit and `0006` as ONE atomic change; run `alembic check` before pushing.** Do NOT
edit `0001`–`0005` (migration history is immutable; the delta is `0006` only).

`canonical_alias` is populated on **merge** (survivor + every collapsed/prior id) AND on **split** (anchor
side keeps its durable id; ejected id recorded as a traceable alias — no orphan). A split **adds** an alias
row; it never deletes (append-only).

---

## 7. Adopt / merge / split design

Hooks run **after** `cluster_and_merge` returns (after the ADR-0037 resolve), **before** the pipeline writes
(`pipeline.py` between line 294 and the promote loop):

- **adopt** (person-neutral, slice-1) — a re-ingested member carrying an anchor adopts the existing durable
  id for that anchor (ledger read); no new node, no id churn.
- **merge** (person-neutral auto path for non-sensitive; slice-1) — survivor = `pick_anchor(members)` else
  the min minted id; `canonical_alias` populated for every collapsed id; routed through the **existing**
  guard (`needs_review`, `approved_groups`) — a non-sensitive merge auto-promotes, a sensitive/oversized one
  parks (ADR 0024/0031) exactly as today. The merge survivor id replaces the `wmc-` hash as the cluster's
  `canonical_id` whenever an anchor exists; `wmc-` is the fallback **only** when no anchor and no prior
  ledger entry exist.
- **split** (slice-2, FENCED) — the anchor side keeps its durable id; the ejected id is recorded as a
  traceable alias. **A split of an already-promoted canonical is an un-merge under append-only.** If the
  canonical is sensitive/sanctioned/PEP it crosses into human sign-off (§8). Split-of-sensitive is wired
  through `signoff.approve/reject`, never auto-run.

`graph/writer.py` keys the MERGE on the **durable canonical id** (still native `{id}`, ADR 0042) and
**provides** an alias-on-read helper (`resolve_node_id`/`get_entity_by_alias`) that resolves a superseded id
to the surviving node via the ledger. `build_referent_map` (`referents.py`) maps members to the **durable
id**, not the `wmc-` hash.

> **Alias-on-read scope caveat (slice-1):** the `writer.py` alias-on-read helper is provided and
> integration-tested, but **no live read path consumes it yet** — the Phase-1 read surface
> (`graph/queries.get_entity`/`get_neighbors`/`get_provenance`) and the API/MCP layer still read by bare
> node id and are **out of this gate's scope**. Wiring alias resolution into the live read surface is a
> follow-up gate (backlog H-1); slice-1 delivers the capability + the durable-id write key + ledger, not
> the read-side cutover.

---

## 8. Person-neutral auto path vs sign-off split (the fencing — CRITICAL)

This gate is **pure id-derivation/stability and is person-NEUTRAL when scoped right**. It does NOT touch
`DEFAULT_MERGE_THRESHOLD=0.92`, Splink weights/scores, or who-merges-with-whom (Gate A slice-2). Fences:

1. **Anchor precedence defers to ADR 0040** (§5): never fuse two entities with different QIDs/LEIs/regNos/
   taxNos. Deriving a durable id is never a back-door merge.
2. **Split of an already-promoted canonical is an un-merge** (append-only, no delete). If it re-decides
   membership of a **sensitive/sanctioned/PEP** canonical it requires human sign-off (CLAUDE.md / ADR 0031).
   **FENCED to the existing sign-off path — never auto-run.** Whether the sensitive-split operation ships in
   this gate is the **gated sub-slice (slice-2)**; if it ships it is wired through `signoff`, not as an
   autonomous mutation.
3. **`canonical_alias` is person-neutral** — superseded-id traceability / yente-referent data.

**Slice split:** slice-1 = person-neutral auto path (anchor id-derivation + alias/ledger + adopt +
non-sensitive merge). slice-2 = adopt/merge/split *operations*, with **sensitive-split fenced to sign-off**
if included.

---

## 9. Out of scope (hard stops)

- The incremental/streaming-ER fork of ADR 0019 (still OPEN).
- Changing `DEFAULT_MERGE_THRESHOLD` or any Splink weight/score/blocking rule (Gate A slice-2).
- A new datastore / parallel model; editing migrations `0001`–`0005`.
- Cross-run graph-side sweep of already-persisted edges (Gate C, ADR 0025).
- **Re-keying an anchored *singleton*'s graph node to its durable id across re-ingest.** The durable-id
  re-key + ledger write are gated on `cluster.is_merge` (slice-1): an anchored singleton keeps its raw
  member id as its graph node key, so its *node* still churns across re-ingest (its derived durable id is
  identical). Matching an incoming singleton to an existing persisted node by anchor is **cross-batch
  dedup** — the ADR-0019-deferred incremental-ER surface. slice-1 delivers the durable-id derivation +
  ledger + the merge-path re-key; the singleton graph carve-out is deferred (see ADR 0044 §Consequences).
- GeoNames / OpenCorporates anchor producers.
- Sensitive-split as an autonomous (non-sign-off) mutation — forbidden.

---

## 10. Acceptance criteria — APPROVE / DENY

**APPROVE** requires ALL of:
- A1. `tests/test_stable_id.py` exists, FAILS on `master@9f42852` (the re-ingest `wmc-` id changes), and
  PASSES post-fix. Non-vacuous (real asserts, no skip/xfail).
- A2. **Re-ingest of an anchored entity yields an IDENTICAL durable canonical id** (the crux).
- A3. **adopt** works (re-ingested anchored member adopts the existing durable id; no new node).
- A4. **merge** populates `canonical_alias` (survivor id + every collapsed id).
- A5. **split** keeps the anchor side's durable id AND populates `canonical_alias` for the ejected id (no
  orphan; ejected id traceable).
- A6. The anchor-conflict guard (ADR 0040) is preserved: a two-QID (or two-LEI) cluster never derives a
  durable id from the conflicting tier (parks/falls-through), proven by test.
- A7. `VERIFIED_API.md` present, verbatim, correct module paths (§2).
- A8. `0006` migration ⇄ `models.py` drift guard green; `alembic check` clean; `0001`–`0005` untouched.
- A9. All frozen tests (§11) green byte-for-byte.
- A10. **`wmc-` is provably idempotency-only** — a grep shows `wmc-`/`_CANONICAL_ID_PREFIX` is read as
  durable identity in **no** path (only the unanchored-merge fallback), AND a test asserts an anchored merge
  is NOT keyed `wmc-`.

**DENY** if ANY of:
- D1. The durable canonical id derives from `wmc-<hash>` in **any** path (anchored or via ledger). `wmc-`
  may only be the unanchored-merge fallback fingerprint.
- D2. nomenklatura API used unverified / paraphrased / bound to the wrong module path (§2).
- D3. A re-ingest of an anchored entity yields a **different** durable canonical id (A2 fails).
- D4. `canonical_alias` not populated on merge OR not populated on split.
- D5. Anchor-conflict guard (ADR 0040) bypassed — a durable id picked from a conflicting anchor set.
- D6. A sensitive-canonical split runs **without** the sign-off path (auto un-merge of a sensitive entity).
- D7. `DEFAULT_MERGE_THRESHOLD` / any Splink weight/score/blocking touched; or a frozen test loosened
  (removed assert, added skip/xfail, loosened tolerance).
- D8. Migration drift (`0006` ≠ `models.py`) or any edit to `0001`–`0005`.

---

## 11. Frozen tests (KEEP-GREEN, UNCHANGED — a loosened assert is a DENY)

```
tests/unit/test_resolution.py
tests/unit/test_resolution_canonical_id.py            # see NOTE below — wmc- asserts cover the UNANCHORED path
tests/unit/test_resolution_negative_judgement.py
tests/unit/test_resolution_anchor_conflict.py
tests/unit/test_resolution_distinguishing_evidence.py
tests/unit/test_resolution_multiscript.py
tests/unit/test_resolution_merge_incompat.py
tests/integration/test_resolution_pipeline.py
tests/integration/test_resolution_batching.py
tests/integration/test_b6_resolve_incompat.py
tests/integration/test_b6_signoff_poison.py
tests/integration/test_migrations.py                  # drift guard — must stay green WITH 0006
```

**NOTE on `test_resolution_canonical_id.py`:** its fixtures build **unanchored** companies (name + country
only), so `pick_anchor` returns `None` and the merge still keys on `wmc-` — its assertions
(`canonical_id.startswith("wmc-")`, `canonical_id not in member_ids`) STAY TRUE and the file stays frozen.
The new **anchored**-path behavior is proven by the NEW `tests/test_stable_id.py`, never by editing the
frozen file. (Plus the existing `test_b1_crash_recovery.py` / `test_b1_signoff_idempotency.py` crash-window
proofs must stay green — the fingerprint's retry-convergence is unchanged.)

---

## 12. Locked invariants (must hold across the gate)

- **G1 provenance on every node AND edge** — PRESERVED. The writer still stamps `prov_*` on every node and
  relationship; keying on the durable id changes the node *key*, never its provenance projection.
- **Append-only / no un-merge** — PRESERVED. The ledger is append-only; a split adds an alias row, never
  deletes; a sensitive split is fenced to sign-off (the only place an un-merge of a promoted sensitive
  canonical may happen, with audit + human approval).
- **Canonical-canonical only via the guard** — PRESERVED. Merge routes through the existing
  `needs_review`/`approved_groups` guard; `pick_anchor`'s anchor-conflict deferral (ADR 0040) prevents a
  durable-id derivation from fusing two canonicals around the guard.
- **Resolve to canonical IDs** (CLAUDE.md) — STRENGTHENED. Anchor-preferred ids ARE the canonical
  identifiers (Wikidata Q / LEI), now durable across re-ingest.
- **ER decisions affecting a real person need human sign-off; no silent in-place mutation** — ENFORCED by
  the slice-1/slice-2 split and DENY D6.
- **ADR 0036 crash-retry idempotency** — PRESERVED (the fingerprint still converges on retry).

---

## 13. Failing-test-first design (`tests/test_stable_id.py`)

Test-author writes these; they FAIL on `master@9f42852` and PASS post-fix:
1. **re-ingest of an anchored entity → identical durable id** (pre-fix: fresh member ids → different `wmc-`
   → FAILS). The crux (A2/D3).
2. **adopt** — second ingest of an anchored member adopts the existing durable id; no second node (A3).
3. **merge** — survivor durable id + `canonical_alias` populated for every collapsed id (A4).
4. **split** — anchor side keeps its durable id; ejected id recorded as a traceable alias, no orphan (A5).
5. **anchor-conflict** — a two-QID cluster does NOT derive a durable id from the QID tier (ADR 0040; A6).
6. **MR-5 idempotence** — running the resolve+derive twice over the same anchored input is a no-op (no
   duplicate node, no duplicate ledger/alias row).

**Adversarial target:** a **re-ingest + concurrent split race** — a re-ingest mints fresh member ids while a
human reject ejects a member. The ledger/alias MUST keep the surviving durable id stable AND keep the
ejected id traceable: **no orphan, no silent id churn.** (This is the test the judge weights most heavily.)

---

## 14. Slice plan (each individually mergeable + CI-green)

The id-derivation change is cross-cutting (like Gate 0), so it is front-loaded into slice-1.

- **slice-1 (person-NEUTRAL, the bulk; autonomously buildable + mergeable):** `resolution/canonical.py`
  (`pick_anchor` + ledger helpers + `wm-mint-<uuid>`) + the `canonical_id_ledger` model in `db/models.py` +
  migration `0006` + anchor-preferred derivation wired into `merge.py`/`pipeline.py` + alias-on-read in
  `graph/writer.py` + referent-to-durable-id in `referents.py`. The precedence list (§4) lands here. Proves
  A1–A5, A7–A10. Changes NO person-affecting value. **LAND FIRST.**
- **slice-2 (adopt/merge/split operations, with sensitive-split FENCED to sign-off if included):** the
  explicit adopt/merge/split operation surface beyond the in-pipeline derivation, and — IF it ships —
  sensitive-canonical split wired **through `signoff.approve/reject`** (human sign-off; never auto-run;
  versioned with audit). If the sensitive-split operation is not needed yet it is **DEFERRED** and slice-2
  is just the non-sensitive operation API. **LAND SECOND.** `human_fork=true` for the sensitive-split path.

---

## 15. Mechanical risks (the human checkpoint)

1. **Migration drift** (the gate's #1 mechanical risk, ADR 0030): `0006` and the `models.py` ledger model
   must agree byte-for-byte; write them atomically and run `alembic check` before pushing.
2. **Frozen `test_resolution_canonical_id.py`** still asserts `wmc-` on the **unanchored** path — the builder
   must NOT touch it; the anchored path is proven by the new test only. A reflexive "update the old test"
   is a DENY (D7).
3. **Anchor-conflict bypass** (D5/D6): the most dangerous failure is `pick_anchor` silently choosing `[0]`
   from a conflicting anchor set (fusing two real entities into one stable id), or a sensitive split running
   without sign-off. Both are person-affecting and are the judge's top scrutiny.
