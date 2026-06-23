# WorldMonitor — Production-Readiness Audit (2026-06-23)

> **Read-only adversarial audit. Findings only — nothing in this report was fixed.**
> Each finding is a candidate gate for the maintainer to decide on *after* reading.
> Scope hard-stops respected: Gate B / Gate C / S4 untouched; G3 not started; no source file changed.

## Method (why this is not a re-print of `ARCHITECTURE_REVIEW.md`)

This audit was commissioned because the repeated lesson of this build is that **curated tests and
checkers went green while real-scale data exposed real bugs** (the G4 tenant-resolver leak, the H3
edge-drop, the `_flatten` cross-script miss, the eponymous over-merge, the Neo4j config crash). So
the test suite was treated as *non-evidence*, and the existing `docs/ARCHITECTURE_REVIEW.md` (which
is dated "master after WS1–WS2" and is now many commits stale — H3 since fixed, `splink_model.py`
rewritten for ADR 0035, all line numbers drifted) was treated as an **untrusted lead list**, not
ground truth. Every claim below was re-derived against the *current* code.

Work was decomposed into six parallel read-only investigation threads (invariants; control/data-flow
& silent-failure; ER correctness; security; deferred-ledger & cross-store; operational readiness).
The load-bearing findings were then **personally verified by the author**: the files
`resolution/pipeline.py`, `resolution/merge.py`, `resolution/splink_model.py`, `resolution/signoff.py`,
`provenance/model.py`, and `runner/driver.py` were read in full, and **four claims were reproduced at
runtime** against the project venv (marked ⚑ REPRODUCED below). The remaining `file:line` citations
were cross-checked by a clean-context reviewer before this report was finalized.

Severities: **BLOCKER** = must fix before building on the system; **HIGH** = fix before relying on
the affected path in production; **MEDIUM/LOW** = scheduled debt. "Would tests catch it?" is answered
for every finding because the suite's green status is explicitly not trusted here.

---

## Verdict at a glance

**NOT production-ready to build on yet** — but the foundation is sound and the gaps are concentrated,
not pervasive. The architecture's invariant scaffolding is real and several past bugs are genuinely
closed (G4 resolver leak, H3 edge-drop, the old "B1" loop-kill — all re-verified fixed below). The
blockers are four concrete clusters: a crash-window that silently corrupts the canonical graph; a
poison-batch class that wedges a tenant's queue; silent ER over-merge of *distinct* real entities; and
the absence of backup/restore + process supervision. None require architectural change — each has a
contained fix. Full verdict and must-fix shortlist at the end.

| Sev | Count | IDs |
|---|---|---|
| BLOCKER | 4 | B-1 … B-4 |
| HIGH | 8 | H-1 … H-8 |
| MEDIUM | 10 | M-1 … M-10 |
| LOW | 7 | L-1 … L-7 |
| Known/deferred (confirmed still-silent) | 1 | D-1 (G3) |

---

# BLOCKERS

## B-1 — Cross-store write-before-commit + non-deterministic canonical id → silent duplicate/orphan canonical nodes; lost human-sign-off durability
**The graph is the product; this corrupts it under a crash with no audit trail.**

**Evidence (verified directly):**
- Pipeline path: `resolution/pipeline.py:287` `write_entities(neo4j, promoted_entities, tenant_id=...)` runs and **commits to Neo4j inside `_resolve_batch`**, while the Postgres commit (queue status + `merge_audit`) is at `resolution/pipeline.py:125` in the *caller*, **after** the batch returns.
- The canonical id is **non-deterministic**: `resolution/merge.py:135` `resolver.get_canonical(entity_id)` → nomenklatura `resolver/resolver.py:426` `canonical = Identifier.make()` (random shortuuid). The resolver is per-batch ephemeral (`merge.py:59-75`), so the chosen id is never persisted.
- Sign-off path has the same ordering: `resolution/signoff.py:162` (approve) and `:197` (reject) call `write_entities` **before** `session.commit()` at `:169` / `:204`.

**Failure scenario:** A batch merges sources {A,B} into `NK-x1`, the writer commits node `NK-x1` to Neo4j, then the Postgres commit at `pipeline.py:125` fails (transient DB blip, connection reset, or the unsupervised driver process — see B-4 — is killed in the window). Postgres rolls back: the queue rows stay `pending` and the `merge_audit` row vanishes. The next resolve pass re-loads the same rows and re-mints a **different** `NK-x2`. Neo4j now has *two* canonical nodes for one real entity; `MERGE` idempotency keys on `id`, so it cannot collapse them, and `NK-x1` is an **orphan with no audit row** — directly violating "de-dupe before counting" and the "merge audit trail, never silent" invariant. For sign-off, a crash between `signoff.py:162` and `:169` leaves the promoted entity live in the graph while Postgres still shows `pending_review` with **no `ResolverJudgement` and no `sign_off` row** — the human decision CLAUDE.md mandates for a change affecting a real person is silently absent, and the merge can be re-actioned.

**Recommended fix (described, not implemented):** Make the un-anchored canonical id **deterministic** — derive `NK-` from a stable hash of the sorted member-id set instead of `Identifier.make()` — so a re-run MERGEs onto the same node and the duplicate cannot form. Independently, reorder so Postgres status/audit commits before (or transactionally with) the graph write, or introduce an outbox/"graph-written" marker so a crash between the two stores is reconcilable. Add an idempotency key to the sign-off decision.

**Confidence:** High (ordering, durable Neo4j commit, and random id all read directly).
**Tests:** No — `test_resolution_batching.py` re-runs only *after* rows are already `resolved`; no test injects a post-Neo4j-write Postgres-commit failure, and no fixture re-resolves still-`pending` rows.

---

## B-2 — Poison batch: any uncaught exception inside `_resolve_batch` strands the tenant's entire pending queue forever (no dead-letter)
**A single malformed row silently halts all resolution for that tenant.**

**Evidence (verified directly + ⚑ REPRODUCED):**
- `resolution/pipeline.py:202` `entities = [make_entity(item.raw_entity) for item in items]` — **unguarded**; `make_entity` raises `InvalidData` on a missing/unknown schema. This runs *before* the "invalid"-quarantine safety sweep (`pipeline.py:260-276`), so the sweep never executes.
- `resolution/pipeline.py:205` `pairs = score_pairs(entities)` — **unguarded**. ⚑ REPRODUCED: `score_pairs([sanction_a, sanction_b])` on two no-name `Sanction` entities raises `SplinkException` (the `substr(name_fp,1,4)` blocking rule over an all-NULL `name_fp` column). The queue is ordered by `created_at` (`pipeline.py:109`), so a window of standalone `Sanction`/`Address`/`Position` rows — which OpenSanctions emits in quantity — clusters into one batch and trips it.
- A `by_id[left_id]` lookup in `splink_model.py:205` can also `KeyError` (NULL `entity_id` rows are not deduped — see L-4).
- When `_resolve_batch` raises, `_resolve_tenant` catches and logs (`driver.py:262-264`), but `resolve_pending`'s `session.commit()` (`pipeline.py:125`) never runs, so the rows stay `pending` and the **same batch re-loads every pass** — the tenant's whole queue is wedged behind it, with only a recurring WARNING and no dead-letter/`invalid` row.

**Failure scenario:** A connector enqueues one row whose `raw_entity` has a schema FtM later renames, or a batch window of no-name entities forms. Every subsequent resolve tick for that tenant aborts on the same batch; no valid row behind it ever resolves; `queue_pending` climbs forever; nothing pages (see H-8).

**Recommended fix:** Build entities and score per-batch defensively — wrap `make_entity` per row and `score_pairs` per batch; on failure set the offending rows `invalid` and write a dead-letter capturing `source_record` + the exception, so the bounded drain always makes progress. The safety sweep must cover construction/scoring failures, not just None-id.

**Confidence:** High. **Tests:** No — `test_unresolvable_id_row_is_quarantined` uses a valid schema with `id:None`, exercising only the post-cluster sweep; no fixture trips `make_entity` or an all-no-name `score_pairs`.

---

## B-3 — Silent ER over-merge of *distinct* real entities (generic-token fingerprint collapse + registration-number blindness)
**Conflates distinct legal persons into one canonical node, unflagged, on real corporate/sanctions data.**

**Evidence (verified directly + ⚑ REPRODUCED):**
- `resolution/splink_model.py:132-133` keys matching on `fingerprints.remove_types(fingerprints.generate(entity.caption))`. ⚑ REPRODUCED: `remove_types` strips generic descriptors, not just legal forms — `"International Trading Co Ltd"` and `"Import Export Trading Co Ltd"` **both → `"trading"`**. Two distinct orgs hit the *exact* name level (`splink_model.py:59`, m=0.99).
- `_flatten` (`splink_model.py:143-149`) projects only `name_fp / country / birthDate / wikidataId`; the comparison set (`splink_model.py:182-189`) has **no `registrationNumber`/`taxNumber`**. ⚑ REPRODUCED (and corroborated by ADR 0035 for the Legion pair): two `Company` rows, same name + `country`, different `registrationNumber`, score **0.9825** and merge.
- The catastrophic-merge guard fires only on *sensitivity* or size > 10 (`resolution/review.py`). A same-trade-name **non-sensitive** pair is **not flagged** (`pipeline.py:226-229`) → auto-merged with no review, no alert, no audit beyond a `decision="merged"` row.

**Failure scenario + quantified impact:** Corporate registries (us company data, EU/UK `ext_*` registers, CN/HK exporter lists) routinely contain *many* distinct active companies sharing a trade name in one country, and many whose only non-legal-form token is itself generic (`trading`, `group`, `holdings`, `general`). At OpenSanctions scale this systematically fuses unrelated legal entities into single canonical nodes — corrupting the resolved graph that *is* the product. ADR 0035's validation ("us_dod_chinese_milcorps 735×`Co Ltd` → 0 merges") only proves names with *distinct surviving brands* don't collide; it never tested generic-residual collapse or non-sensitive same-name auto-merge.

**Recommended fix:** Add `registrationNumber`/`taxNumber` as a **distinguishing/negative-evidence** comparison level (a present-but-clashing id drops the pair below threshold); require a minimum distinguishing-token count before treating a `remove_types` fingerprint as an exact key; never use an over-stripped generic token as the sole match key.

**Confidence:** High (reproduced end-to-end). **Tests:** No — every over-merge fixture marks entities `sensitive=True`, masking the auto-merge behind the guard; no fixture uses two distinct *non-sensitive* same-name orgs or generic-residual names.

---

## B-4 — No backup/restore for any store; the driver is unsupervised and not even in compose; `/health` cannot detect a dead pipeline
**A single ordinary operational event (reboot, OOM, `down -v`) causes silent total loss or a silent full stop.**

**Evidence:**
- `deploy/compose.yaml:165-171` declares named-but-unbacked volumes (`postgres-data` / `neo4j-data` / `minio-data`) with no bind mount to a host path, no backup sidecar, and no dump tooling; a repo-wide search for `backup`/`restore`/`dump` finds nothing. The three stores are written at different times with no 2-phase coordination (B-1), so even *with* backups, three independently-timed snapshots restore mutually inconsistent (Postgres `resolved` but the graph node missing, or vice-versa).
- `deploy/compose.yaml` defines only `postgres/neo4j/minio/redis/zitadel` (all `restart: unless-stopped`); there is **no worldmonitor app/driver service and no Dockerfile**. The driver is launched by hand per the runbook (`docs/runbooks/smoke-run.md`) as a foreground host process — nothing restarts it on host reboot, lost shell, or OOM-kill.
- `api/main.py:47-50` `/health` returns `{"status":"ok","environment":...}` without touching any store (a pure liveness echo); the driver exposes no health/metrics surface at all.

**Failure scenario:** The driver is OOM-killed (very reachable — see H-6) or the host reboots. The five backing services come back green, `/health` still says `ok`, `smoke_metrics` still prints — but no ingest and no resolution happen, and nothing pages. Separately, a `docker compose down -v` or a disk failure destroys the entire graph + queue + landing zone with zero recovery.

**Recommended fix:** Containerize the driver and add it to compose with `restart: unless-stopped`, `depends_on: service_healthy`, and a `mem_limit`; add a real `/ready` that checks each store and a driver liveness heartbeat (last-tick timestamp). Add a backup runbook + scheduled `pg_dump` / `neo4j-admin database dump` / `mc mirror`, persist volumes to a known host path, and document a restore order anchored on Postgres as the source of truth (replay resolution from the queue/audit).

**Confidence:** High. **Tests:** No — deployment/supervision is outside the test surface (`run_forever` is `# pragma: no cover`); a test even asserts `/health`=ok, which is exactly the false-confidence signal.

---

# HIGH

## H-1 — Human NEGATIVE judgement silently overridden by a transitive Splink bridge
**Evidence (⚑ REPRODUCED):** `resolution/merge.py:105-130` skips only the *exact* decided pair (`if key in decided_pairs: continue`, `:116`); it never calls `resolver.get_judgement`/`check_candidate` before applying a Splink positive. nomenklatura's `_traverse` (`resolver/resolver.py:234`) follows **positive judgements only**, and `get_canonical` = `max(connected)` — so a positive chain transitively fuses a rejected pair, and `decide(POSITIVE)` (`resolver.py:421-430`) canonicalizes blindly with no negative-conflict check. ⚑ REPRODUCED: `cluster_and_merge([A,B,C], pairs=[A~B 0.99, B~C 0.99], judgements=[NEGATIVE(A,C)])` → a single canonical with members `('A','B','C')` — the human reject was silently undone.
**Failure scenario:** An operator rejected merging two distinct same-named PEPs A and C. A later batch contains a third near-duplicate B that Splink scores ≥0.92 to both → A and C re-fuse. For a **non-sensitive** pair this auto-merges silently (a true override); for a **sensitive** pair the guard re-parks the fused cluster (caught, but contradicting "a rejected pair never re-merges"). Either way the durable-judgment guarantee the project just validated (reject → no-re-park) does not hold under a realistic 3-way bridge.
**Fix:** Before applying each Splink positive, consult `resolver.get_judgement`/`check_candidate` and skip pairs that resolve to NEGATIVE (nomenklatura's negative is transitive-aware); or post-validate every cluster against stored negatives and split/re-park on contradiction. Correct the `merge.py:88-91` docstring, which overstates the guarantee.
**Confidence:** High. **Tests:** No — `test_signoff.py` proves only the *exact* rejected pair stays split; no fixture adds a bridging third record.

## H-2 — Schema-incompatible cluster member silently dropped from the merge but retained in `member_ids` → data loss + mis-rewired edges + false "resolved"
**Evidence (verified directly):** `resolution/merge.py:162-177` catches `InvalidData` and only **logs** a skipped member; the member stays in `cluster.member_ids` (fixed at `merge.py:144`). Downstream, `pipeline.py:254` `_set_status(cluster.member_ids, "resolved")` marks the dropped member's queue row `resolved`, `audit` claims it merged, and `build_referent_map` (`pipeline.py:284`, `referents.py`) rewrites references to it onto the (wrong-type) canonical id. `_schema_compatible` is pairwise (`splink_model.py:152-167`), so a transitive cluster (Org~Company~Person, with Org⊥Person) reaches `_merge_entities`.
**Failure scenario:** A Person transitively gathered into a Company canonical is dropped from the merged entity, yet its row is `resolved`, audit says merged, and edges pointing at the Person (e.g. `Directorship.director`) are rewired to a *Company* node — a vessel/person's relationships silently re-attributed, with only a WARNING as "audit". ADR 0035 claims this defensive skip is sound; it is incomplete.
**Fix:** On skip, **remove** the member from `member_ids` and re-emit it as its own singleton (correct status/referents/node), and record the skip durably (dead-letter/audit field), not just a log.
**Confidence:** High (control flow read directly). **Tests:** No transitive-incompatible fixture exists.

## H-3 — Provenance collapses to one source on an N-source merged node (partial G1 / GDPR)
**Evidence (verified directly):** `provenance/model.py:41-46` `_context_scalar` returns `str(value[0])` — first element only. FtM `merge_context` accumulates all sources into a list, so a node fused from OFAC+EU+UN carries on the graph only the first member's `prov_source_id`/`prov_source_record` (`provenance_node_properties` → `writer.py:165`). The other N-1 sources' lineage (including their landing pointers) is unreachable from the graph — only from `raw_entity`/`MergeAudit`. Edges are fine (single asserting source).
**Failure scenario:** A subject-access/audit query against a multi-source canonical node under-reports its provenance — the GDPR/audit log the invariant promises is incomplete for exactly the multi-source merges that are the platform's core function.
**Fix:** Project multi-valued provenance (a `prov_source_ids` array, or reified `:Provenance` sub-nodes per source); stop truncating to `[0]` in the node projection.
**Confidence:** High. **Tests:** No — `test_provenance.py`/`test_graph_writer.py` only round-trip single-source entities.

## H-4 — Abjad (Arabic/Persian) ER both under- and over-merges — the primary sanctions population is mis-resolved
**Evidence (⚑ REPRODUCED):** `splink_model.py:130-133`. Under-merge: the **same** person transliterated two ways yields different keys — `"Mohammed Al-Zawahiri"` → `al mohammed zawahiri` vs `"Muhammad Al-Zawahri"` → `al muhammad zawahri`. Over-merge: distinct names collapse to near-identical consonant skeletons — `كريم`(Karim)→`krym` vs `كرم`(Karam)→`krm`; `سالم`(Salim)→`salm` vs `سلم`(Salam)→`slm`.
**Failure scenario:** Arabic-script entities are exactly the high-value targets on OFAC/EU/UN lists. Under-merge leaves duplicate sanctioned persons un-fused across feeds (defeating the platform's purpose); over-merge mis-attributes one sanctioned person's edges to another. ADR 0035 defers this ("LogicV2 follow-up") and over-generalizes the Cyrillic win to "Arabic/Chinese sources" — abjad has no deterministic transliteration, so the fix does not hold there. This is a latent correctness hole in the core use case, not an optional refinement.
**Fix:** Key abjad names off a consistent bare-script skeleton (not the Latin transliteration), or add the planned `LogicV2` post-blocking re-scorer; until then, document Arabic-name ER as known-degraded in the runbook.
**Confidence:** High. **Tests:** No Arabic/Persian fixture exists.

## H-5 — `wikidata_id` exact level overrides total name disagreement → one bad anchor fuses unrelated entities
**Evidence (⚑ REPRODUCED):** `splink_model.py:188` `_exact_comparison("wikidata_id", m=0.999, u=0.000005)`. Two Persons with **completely different names** but the same `wikidataId=Q42` score **0.9795** and merge — the wikidata level dominates and the name "else" level (m=0.04) cannot veto it.
**Failure scenario:** Wikidata ids enter via the enrichment path (`pipeline.py:255`). One mis-mapped/over-broad Q-id (an org and its eponymous foundation), or an enricher bug assigning a shared anchor, silently fuses entities that disagree on every other feature. With `u=0.000005`, even a coincidental shared id reads as near-certain identity.
**Fix:** Cap the wikidata contribution so it cannot single-handedly clear 0.92 against an active name *disagreement* (require name corroboration), or set `u` to a realistic id-collision rate.
**Confidence:** High. **Tests:** No fixture combines a shared wikidata_id with disagreeing names.

## H-6 — GeoNames loads the whole country zip into memory + materializes every line → OOM kills the shared driver
**Evidence:** `plugins/connectors/geonames/connector.py:120-125` `httpx.get(...)` then `zipfile.ZipFile(io.BytesIO(response.content))` (whole zip in RAM) then `archive.read(f"{country}.txt").decode("utf-8")` (whole decompressed text), then `connector.py:80` `text.splitlines()` materializes a full list. Peak ≈ compressed + full decoded string + full list simultaneously. The driver runs unsupervised (B-4) with **no `mem_limit`** (only Neo4j has one).
**Failure scenario:** `US`/`CN`/`IN` decompress to hundreds of MB; `allCountries` is an instant kill. One tenant's GeoNames pass OOMs the shared single-node driver → all tenants stop, and nothing restarts it (B-4). The runbook already warns operators to avoid large GeoNames batches — the edge has been hit but not fixed.
**Fix:** Stream the download to a temp file and iterate the zip member lazily (`io.TextIOWrapper(archive.open(member))`) instead of `read().decode().splitlines()`; the ingest path is already windowed, so a streaming `collect()` bounds memory to one window.
**Confidence:** High. **Tests:** No — tests use the tiny `path` override; the prod `_download` path is never load-tested.

## H-7 — GeoNames `path` config = arbitrary server-file read (LFI)
**Evidence:** `plugins/connectors/geonames/config.schema.json` constrains `country` to `^[A-Za-z]{2}$` (line 13) but the `path` property is an unconstrained `"type":"string"` (lines 15-19; no pattern, no base-dir confinement); `geonames/connector.py:75-77` does `Path(str(local_path)).read_text("utf-8")` verbatim with no size bound.
**Failure scenario:** Whoever can author a geonames `ConnectorInstance` config (today an operator; **any tenant** once the planned self-service connector-config API ships — the plugin framework specs `config.schema.json` as the UI-form driver) sets `path:"/proc/self/environ"` or `/etc/passwd`; each line lands as an `Address` raw record in the tenant's own readable landing zone, exfiltrating `CONFIG_ENCRYPTION_KEY`, `NEO4J_PASSWORD`, `MINIO_SECRET_KEY`, DSNs.
**Fix:** Remove `path` from the production schema (dev-only) or gate behind an env flag; if kept, require the resolved real-path to be inside an allowlisted base dir and bound the read size.
**Confidence:** High (LFI mechanically certain; reachability gated by who can author config today). **Tests:** No negative test asserts `path` traversal is rejected.

## H-8 — No alerting; failed connectors are never retried in-loop; recovery runs only at startup
**Evidence:** Observability is a `task_run` table + the manual `smoke_metrics` one-liner (`runner/smoke_metrics.py`) + plain logs (`logging.basicConfig`, `driver.py:354`) — no metrics endpoint, no structured logs, no alert hook. `_finalize` sets a failed instance `status="error"` (`driver.py:292`), which no longer matches the due-query (`ConnectorInstance.status == "enabled"`, `driver.py:152`) → **never retried** by the running process. `recover_stale` and `prune_task_runs` are invoked **once**, at startup (`driver.py:302-303`); a hung resolve holds the non-blocking `_resolve_lock` (`driver.py:228`) and starves *all* tenants' resolution with no mid-life reconcile; `recover_stale` does not advance `next_run` (`driver.py:99-110`), so a process-killing connector crash-loops with no backoff.
**Failure scenario:** A transient failure (Neo4j restart, a 503, one bad decrypt) darkens a connector permanently until a human re-enables it and restarts the driver; a growing `ingest_dead_letter`, piling-up `parked_merges`, or resolve-falling-behind-ingest degrade silently for days. Nothing pages.
**Fix:** Re-enable `error` instances after a backoff (advance `next_run`, keep `enabled`); run `recover_stale`/`prune_task_runs` periodically in-loop; give `resolve_pending` a wall-clock timeout and escalate repeated lock-skips; expose the `smoke_metrics` counters as a metrics endpoint with alert rules (dead-letter rate, queue-growth slope, parked count, last-successful-tick age).
**Confidence:** High. **Tests:** No — recovery/retry consequences over a long-lived run are not modeled.

---

# MEDIUM

**M-1 — `SET n = props` full-overwrite clobbers prior anchors/provenance on any re-emit.** ftmg `transform.py` writes nodes with `SET n = props` (full replacement, not `+=`); the writer injects anchors+prov into the same flat params (`graph/writer.py:84-93`, `:161-166`). A later, thinner re-emission of the same `{id, tenant_id}` (a sparser source variant, or the B-1 re-resolve) silently erases the prior node's anchors/`prov_*`. Fix: rewrite the generated node query to `SET n += props` in `_tenantize_query`, or guarantee re-emits carry the full superset. *Bites within single-node v0 on any re-ingest, not only at Gate C.*

**M-2 — Weak/placeholder secrets in the live `.env`.** `.env` carries `ZITADEL_MASTERKEY=00000000000000000000000000000000` (all-zeros = effectively no encryption of Zitadel's OIDC signing material) plus guessable service passwords (`worldmonitor`/`worldmonitor123`), and compose publishes backend ports (5432/7687/9000/6379/8080) to the host. Anyone with the Postgres volume + this known masterkey can forge tokens for any org → since `tenant_id` is the verified org claim, that forges tenancy. Fix: random masterkey + strong unique passwords; bind backend ports to the internal network; assert non-placeholder masterkey when not `development`.

**M-3 — `.env.example` ships `MERGE_GUARD_MODE=alert`.** The code default is `block` (`settings.py:52`) but `BaseSettings` reads `.env`, and the documented "copy `.env.example` to `.env`" makes a standard deployment boot **fail-open** — writing flagged PEP/sanctioned merges with no review. Fix: set `block` (or comment it out) in the example.

**M-4 — Migration adoption heuristic blind-stamps on a single column check.** `db/engine.py:70-76` stamps a DB at `head` if `er_queue_item.entity_id` exists, with **no verification that later-revision objects (the 0003 `resolver_judgement`/`sign_off` tables) exist**. An out-of-band partial/restored schema is stamped at head → runtime failure on a missing table with no migration that will ever create it. (Largely mitigated for the *upgrade* path by single-transaction DDL in `migrations/env.py`, but the stamp path is unguarded.) Fix: compare live schema to `Base.metadata` before stamping; else upgrade.

**M-5 — Migrations are unguarded for online execution; no tested rollback.** `0002_runway.py` does `add_column` + `create_unique_constraint` on `er_queue_item` inside one transaction (`migrations/env.py`), taking an `ACCESS EXCLUSIVE` lock + full scan. Against a multi-million-row queue this stalls the driver's enqueue path; no `CREATE INDEX CONCURRENTLY`, no `lock_timeout`, no rollback runbook. Fix: concurrent/`NOT VALID`+`VALIDATE` patterns (need `transaction_per_migration`), a `lock_timeout`, and a migrate-while-stopped procedure.

**M-6 — Unbounded storage growth: landing-zone orphans + no dead-letter retention.** `landing.put` (`ingest.py:164`) precedes the windowed commit, so a mid-window crash orphans S3 objects with no GC; re-runs with a non-deterministic `record.key` accrete orphans permanently. `ingest_dead_letter` has no retention (`task_run` is pruned, but only at startup — H-8). Disk eventually fills → `landing.put` fails → ingest halts (no alert). Fix: landing-zone GC/lifecycle keyed on committed `source_record`; dead-letter retention; disk-usage alert; document `record.key` must be deterministic.

**M-7 — Silent drops record only aggregate logs, not per-row evidence.** The `invalid`-quarantine sweep (`pipeline.py:271-276`) and the schema-skip (H-2) log only counts/warnings — no `source_record`, no dead-letter — so dropped records are untraceable, weakening the audit-on-failure posture `IngestDeadLetter` otherwise upholds. Fix: per-row dead-letter with `source_record`.

**M-8 — GDS degree projection uses a fixed, non-tenant-scoped catalog graph name.** `graph/gds.py:14` `_DEFAULT_GRAPH = "wm-degree"` (constant); the projection Cypher is tenant-filtered but the catalog name is shared. Two overlapping analytics runs (or one crashing before the `finally`-drop) collide or stream/drop another tenant's projection — a cross-tenant leak. Fix: tenant-unique graph name (`f"wm-degree-{tenant_id}"`).

**M-9 — Cross-host redirects + decompression/parse DoS on outbound fetches.** Both connectors pass `follow_redirects=True` (`opensanctions/connector.py:59`, `geonames/connector.py:121`); a poisoned upstream/DNS can redirect into the deploy network (Neo4j/MinIO/Zitadel/metadata). Plus unbounded `json.loads` per source line (`opensanctions/connector.py:74`) and the GeoNames zip (H-6) are decompression/parse-bomb surfaces. Inputs are now regex-constrained (good — see VERIFIED SOUND), so this is a residual amplifier. Fix: `follow_redirects=False` (these endpoints serve 200 directly) or allowlist redirect targets + block RFC1918/link-local; cap per-line/zip size.

**M-10 — `ConfigCipher` has no key rotation.** `db/crypto.py:21` uses a single `Fernet`, not `MultiFernet`. Rotating `CONFIG_ENCRYPTION_KEY` orphans every stored config (decrypts throw `InvalidToken` at `driver.py:196`) — a fleet-wide ingest outage with no migration path. Empty-key rejection *is* correctly enforced. Fix: `MultiFernet([new, old])` for overlap rotation.

---

# LOW

**L-1 — `guard_mode` fails open to "alert" on an unknown string at the function boundary.** The env path is fail-fast (`Literal["alert","block"]`, pydantic rejects typos; `driver.py:260` passes no override), but `resolve_pending`/`_resolve_batch` typed `str` fall through to alert behavior on any non-`"block"` value (`pipeline.py:232,242`). Latent against a future programmatic caller. Fix: fail-closed (`mode != "alert"` → park) and type the param as a `Literal`.

**L-2 — `ensure_bucket` misclassifies any `ClientError` as "missing".** `storage/landing.py:69-74` bare-catches `ClientError` and calls `create_bucket`, so a 403/transient 5xx is reinterpreted as absent, masking the real status. Fix: branch on `Error.Code`; only create on 404/`NoSuchBucket`.

**L-3 — `resolver_judgement` has no `CHECK (left_id <= right_id)`.** Ordering is caller-enforced (`signoff.py:115` sorts; verified all current writers correct). A future writer inserting `(C,A)` for an existing `(A,C)` evades the pair-unique constraint and can create a contradictory judgement. Fix: add the DB CHECK.

**L-4 — NULL `entity_id` bypasses `uq_er_queue_dedup` (and can `KeyError` in `score_pairs`).** Postgres treats NULLs as distinct (`db/models.py:46-48`), so id-less entities double-enqueue on restart and can collide `by_id` in `splink_model.py:205` (feeding B-2). Fix: a deterministic surrogate dedup key, or guard the lookup.

**L-5 — DuckDB `Linker` rebuilt per batch and never closed; block explosion on common `name_fp`.** `splink_model.py:198` constructs `Linker(..., db_api=DuckDBAPI())` per batch with no close; `block_on("substr(name_fp,1,4)")` (`:192`) can make large same-prefix blocks costly. Bounded by `resolve_batch_size`, but resolve-falls-behind-ingest is undetected (H-8). Fix: context-manage/close DuckDB per batch; finer blocking; per-pass timing metric.

**L-6 — G4 has no database-level enforcement.** No Neo4j RLS, no tenant FK/composite PK; isolation rests on every caller's `.where(tenant_id==...)`. All current callers verified scoped (see VERIFIED SOUND), but one future missed filter silently crosses tenants — structural for a "tenant-scoped from day one" product. Fix: consider RLS / a query-builder that injects the tenant clause.

**L-7 — Residual hygiene.** Resource lifecycle leaks (Engine/Neo4j driver never disposed over a long-lived process); mixed DB-clock vs app-clock timestamps can skew durations; `registry.discover_module` has no per-connector error isolation (one bad `__init__` aborts the whole registry, `driver.py`); `settings.sqlalchemy_dsn` only rewrites `postgresql://` (other schemes pass through), and `get_settings()` is `@lru_cache`-memoized so post-first-read env changes are ignored. Each is low-impact but worth a sweep.

---

# Known / deferred — confirmed still SILENT

**D-1 (G3) — abstract `Thing`-range entity links (e.g. `Sanction.entity`) are dropped silently.** ftmg's `generate_entity_links` skips a link whose `prop.range` is the abstract `Thing` schema (it is absent from `config.nodes.schemata` → `continue`), so an OFAC `Sanction` whose `entity` points at the sanctioned `Person`/`Company` yields both nodes but **no relationship between them** — the single most important edge in sanctions data — with no error/dead-letter. This is the explicitly-deferred G3 (ADR 0023) and is **about to be worked on** per the maintainer; flagged here only to confirm it is still silent. The fix direction (resolve the concrete endpoint schema from the referenced entity rather than the abstract range) is noted at `graph/writer.py`.

---

# Reconciliation with the stale `ARCHITECTURE_REVIEW.md`

| Prior finding | Current status |
|---|---|
| **B1** (a failure kills the whole driver loop) | **FIXED** — `run_due_ingests` (`driver.py:161-165`), `run_resolution` (`:242-246`), and `run_forever` (`:307-318`) each wrap work in try/except; `_finalize` failures are caught. Do not carry forward. |
| **H3** (entity-link `entity:`-prefix drop) | **FIXED** — `graph/writer.py:116-134` `_align_entity_link_ids` strips the prefix; `test_entity_link_materialization.py` pins it. (Corroborated by 3 threads.) |
| **H1/H2** (cross-store write-before-commit) | **STILL-REAL** → escalated to **B-1** (the non-deterministic id makes the duplicate un-dedupable). |
| **H4** (provenance collapse on merge) | **STILL-REAL** → **H-3**. Corrected lines `provenance/model.py:41-46`. |
| **H5** (SSRF config→URL) | **OVERSTATED/FIXED** — `dataset`/`country` now regex-bound. Residual redirect amplifier → **M-9**; new `path` LFI → **H-7**. |
| **H6** (landing-key tenant escape) | **FIXED** — `ingest.py:47-57` `_safe_segment` slugifies `record.key`/`dataset` (verified sound). |
| **H7** (concurrent double-process) | **OVERSTATED for v0** — `run_forever` drives ingest then resolution serially per tick; real only at deferred X3. |
| **M2** (guard-mode fail-open) | **MOSTLY FIXED** — env is fail-fast (`Literal`); residual function-boundary gap → **L-1**; example footgun → **M-3**. |
| **M4** (partial-migration adoption) | **MOSTLY MITIGATED** (single-tx DDL) — residual blind-stamp → **M-4**. |
| **M7** (GeoNames OOM) | **STILL-REAL** → **H-6** (escalated; it takes down the shared driver). |
| **M9 / L8 / L12 / M10 / L1 / L2** | STILL-REAL at reduced/equal severity → **M-10 / L-5 / H-8 / M-6 / L-4 / L-2** respectively. |

---

# Verified SOUND (positive coverage — re-derived, not assumed)

- **G4 tenant isolation** — the resolver-leak fix is real: the only resolver is the in-memory ephemeral one (`merge.py:59-75`); every queue/judgement/graph read+write is tenant-scoped; the writer hard-fails on a missing/falsy `tenant_id` (`graph/writer.py:58-59,157-158`); `queries.py` reads scope both endpoints. (Residual: no DB-level enforcement — L-6.)
- **OIDC auth is real, not stubbed** — `authz/oidc.py` validates RS256 signature (JWKS), audience, and issuer; `tenant_id` is read from the verified `urn:zitadel:iam:org:id` claim, **never** client-supplied; middleware fails **closed** (401 if no verifier). The graph read endpoints are not yet wired to HTTP routes, so the internal helpers are not yet client-reachable.
- **Landing-key sanitization** (`_safe_segment`, `ingest.py:47-57`) closes the prior tenant-prefix escape.
- **Idempotent enqueue** — `on_conflict_do_nothing(constraint="uq_er_queue_dedup").returning(...)` counts only true inserts; re-ingest after restart is a no-op (except the NULL-`entity_id` hole, L-4).
- **Per-instance / per-tenant / loop failure isolation** (the old B1) is correctly implemented.
- **Edge provenance** is correctly stamped from the asserting entity (`writer.py:188-201`), not endpoints.
- **Ephemeral-resolver / within-batch dedup**, **durable sign-off precedence**, the **guard exemption** (only an exact re-formation of a single approved group is unflagged — no off-by-one), and **`reject()` writing members as new separate nodes** are all sound for their intended (single-node, within-batch) scope.
- **No `eval`/`exec`/`shell=True`**; Cypher is fully parameterized; `.env` is gitignored (only `.env.example` is tracked).
- **Genuinely-safe deferrals**: cross-batch ER (Gate B), inbound-edge restore (Gate C), canonical-canonical routing (S4), and single-writer (X3) are correct deferrals — none introduces a *latent* v0 bug beyond the ones called out above.

---

# Production-readiness verdict

**Verdict: NOT yet production-ready to build on — gated on the four blockers below.** This is a
well-architected v0 with a real, mostly-enforced invariant contract and a credible record of closing
prior bugs (G4, H3, B1 all re-verified fixed). It is *close*. But three of the blockers attack the
core promise — "the resolved entity graph is the product" — by allowing the graph to be silently
corrupted (B-1 duplicate canonicals on a crash; B-3 distinct entities fused on ordinary real data;
H-2 members lost with mis-rewired edges), and the fourth (B-4) means an ordinary operational event
causes silent total loss or a silent full stop. None of these are visible to the test suite, which is
exactly the failure mode this build keeps hitting.

## Must-fix-before-build shortlist (recommended gate order)

1. **B-1 — deterministic canonical id + commit ordering/outbox.** Stops silent graph-dedup corruption under any crash. (Touches `merge.py`, `pipeline.py`, `signoff.py`.)
2. **B-2 — per-row/per-stage exception isolation in `_resolve_batch`** with dead-lettering. Stops one bad row wedging a tenant's whole queue. (Touches `pipeline.py`.)
3. **B-3 — `registrationNumber`/`taxNumber` discriminator + stop using over-stripped generic tokens as a sole match key.** Stops silent over-merge of distinct legal entities. (Touches `splink_model.py`.)
4. **B-4 — backup/restore runbook + supervise the driver (compose service, restart policy, `mem_limit`, real `/ready`).** Survives ordinary operational events. (Touches `deploy/`, `api/main.py`, `runner/driver.py`.)

**Strongly recommended alongside the blockers:** H-1 (transitive-negative guard — the durable-judgment
guarantee just validated has a hole), H-2 (schema-skip must drop the member), H-3 (multi-source
provenance — the audit/GDPR invariant), and H-6 (stream GeoNames — the most likely first OOM).

Each finding above is a discrete decision for the maintainer; nothing here was changed.
