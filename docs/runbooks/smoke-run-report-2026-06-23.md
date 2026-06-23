# Smoke-run report — 2026-06-23 (complete real-data validation)

Follow-up to `smoke-run-report-2026-06-22.md`, which ran on distinct-orgs + non-sensitive
addresses and therefore recorded `graph_edges=0` / `parked_merges=0` as **expected for that
seed**, listing the seed changes needed to exercise edges and parking. This run realizes
exactly those recommendations on real data, **plus** closes the last validation that had only
ever been proven on fixtures: the durable merge→park→reject→split→no-re-park judgement loop.

**Verdict: GREEN.** The full spine ran end-to-end on real OFAC + us_dod data under a *live*
driver with zero errors; edge materialization and block-mode sensitive parking are now
exercised on real data; and a human rejection of a parked sensitive merge splits it durably,
with no re-park across subsequent resolve ticks.

## Setup
- Datasets (tenant `smoke`, both `enabled`):
  - `opensanctions=us_dod_chinese_milcorps` — corporate ownership; supplies the edge-schema
    (`Ownership`) relationships.
  - `opensanctions=us_ofac_sdn` (`limit: 2000`) — supplies the sensitive cross-script
    duplicate (the "Legion Komplekt" pair) that block mode parks.
- Mode: `MERGE_GUARD_MODE=block` (production posture; ADR 0031 default).
- Fixes under test, all merged to `master`:
  - **ADR 0035** — multi-script name fingerprint (`_flatten`/`_name_fingerprint`) + the
    schema-compatibility gate. Determines *which* entities merge.
  - **H3 fix** (`4ebe4cd`) — `graph/writer._align_entity_link_ids` strips the `entity:`
    prefix so concrete-range entity links realign with raw node ids.
  - **ADR 0031** — durable, tenant-scoped sign-off (`resolver_judgement` / `sign_off`); the
    `reject` path writes each member as its own entity and persists a negative judgement.
- Driver: `worldmonitor.runner.driver` running continuously (block mode); metrics via
  `worldmonitor.runner.smoke_metrics`.

## Volumes — full pipeline green
- **Graph nodes: 2536**, **graph edges: 127.**
- **Queue drained:** `queue_pending=0` after resolve passes.
- **Errors: 0** — `task_ingest_error=0`, `task_resolve_error=0`, `ingest_dead_letter=0`.
- **Sustained:** stable across **24 ingest cycles under a live driver with 0 errors** — the
  WS5 resilience held; resolve passes auto-drained the queue with no restart.
- Both connectors ingested clean (`task_ingest_ok`), resolve ran clean (`task_resolve_ok`).

## Investigation 1 — `graph_edges=127`: edge materialization fires (H3 fix holds)

**Expected, and the point of the richer seed.** `us_dod_chinese_milcorps` carries
edge-**schema** `Ownership` relationships, which materialize as `OWNS` edges using raw
endpoint ids (`graph/writer.py`). The 2026-06-22 run had none of these (Andorra `Address` +
4 distinct sanctions entities) — this seed does, and they write. The H3 fix
(`4ebe4cd`) is in the path and non-load-bearing for this seed (see Investigation 2) but
**holds**: nothing regressed, and the `entity:`-prefix realignment is active for any
concrete-range link a future seed introduces.

## Investigation 2 — `ADDRESS_ENTITY=0`: data-shape, not a bug

**Expected for this seed. Not the H3 regression it could be mistaken for.** `us_ofac_sdn`
writes a record's address **inline** — the address materializes as a single `Address` node
(an inline-address entity), **not** as a concrete-range `Person/Org.addressEntity → Address`
link. So there are no `addressEntity` relationships to materialize and the count is correctly
`0`. This is distinct from the bug H3 fixed: the ~1867 dropped address relationships in
`4ebe4cd` came from a *different, richer* shape (explicit concrete-range `addressEntity`
links). The H3 fix is what would let **those** materialize; this seed simply doesn't contain
them. `ADDRESS_ENTITY=0` here means "no such links in the data," not "links silently dropped."

## Investigation 3 — block-mode parking on real data (the Legion pair)

**`parked_merges=1`, on real `us_ofac_sdn` data, no injected sensitivity.** The cross-script
"Legion Komplekt" pair — `NK-6mNvkSFuS8huYiAimBGH4X` and `NK-8WbtGpC3EtBaT4K59w89mQ`, two
distinct `ru` sanctioned orgs each carrying both a Cyrillic and a Latin name — now merges:
the script-stable name fingerprint canonicalizes both `"ООО Легион Комплект"` and
`"LIMITED LIABILITY COMPANY LEGION KOMPLEKT"` → `"komplekt legion"`, so the pair scores
**~0.983** (ADR 0035 records 0.9825) instead of the pre-fix `0.378`, clearing the 0.92 merge
threshold. Because a member is **sanctioned**, the catastrophic-merge guard does **not**
auto-fuse it — block mode **parks** it as `pending_review`.

This corrects the 2026-06-22 conclusion that "OpenSanctions pre-dedups every sanctioned
entity, so a sensitive 2+ cluster cannot form on real data." That was itself an artifact of
the `_flatten` bug (ADR 0035 §Consequence): the buggy `first("name")` projection *missed*
cross-script sensitive duplicates. The sensitive → block → park path **is** reproducible on
real data.

## Investigation 4 — durable judgement loop, end-to-end under a live driver

**The last mechanism that had only been proven on fixtures is now proven on real data.**
With the Legion pair parked (`parked_merges=1`, `queue_pending_review=2`), an operator
**rejected** the parked sensitive merge (canonical `NK-8Wbt…`) via the ADR 0031 sign-off
path. Observed, live:

- The parked cluster **split into two distinct canonical nodes** — both
  `NK-6mNvkSFuS8huYiAimBGH4X` and `NK-8WbtGpC3EtBaT4K59w89mQ` are present in the graph as
  separate entities (reject writes each member as its own entity + its outbound edges, and
  persists a **negative** judgement). This accounts for the node count rising to 2536.
- `parked_merges` went **1 → 0** and `queue_pending_review` went **2 → 0**.
- **No re-park** across subsequent resolve ticks: every later batch seeds its fresh ephemeral
  resolver with this tenant's judgements, so the rejected pair is skipped before clustering
  and never re-forms (ADR 0031 §2; `resolution/pipeline.py` union-find over positive pairs —
  a rejected pair has none, so it stays split).

This validates the **complete** durable-judgment chain — merge → park → reject → split →
no-re-park — on real data and under a continuously-ticking driver, not a single-shot fixture.

## Verdict

**GREEN — complete real-data validation.** End-to-end on real OFAC + us_dod data, 24 live
ingest cycles, 0 errors, 0 dead-letters. Edge materialization fires (`OWNS=127`); the
`ADDRESS_ENTITY=0` is a correct consequence of inline-address data shape; the sensitive
cross-script Legion pair merges at ~0.983 and **parks** under block mode; and a human
rejection splits the parked merge into two durable canonical nodes that never re-park. The
invariants hold on the live stack: provenance written, tenant-scoped, block-mode guard live,
never auto-fusing a sanctioned entity, and a named-operator sign-off trail.

**References:** ADR 0031 (return-to-block + human sign-off), ADR 0035 (multi-script name
canonicalization / `_flatten`), the H3 concrete-range edge fix (`4ebe4cd`), and the
known/deferred gaps recorded there (abjad fingerprints + `LogicV2`, `registrationNumber`
discrimination — ADR 0035 §Deferred; inbound-edge restore on approve — ADR 0031, Gate C).
