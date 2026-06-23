# Smoke-run report — 2026-06-22

> **Superseded for the validation conclusions by
> [`smoke-run-report-2026-06-23.md`](smoke-run-report-2026-06-23.md).** This run's
> `graph_edges=0` / `parked_merges=0` were *correct for its seed* (distinct orgs +
> non-sensitive addresses) and its closing section listed the seed changes needed to
> exercise edges and parking. The 2026-06-23 run applies exactly those (us_dod ownership +
> the us_ofac Legion pair) and validates edge materialization, real-data sensitive parking,
> and the durable reject→split→no-re-park loop. Keep this report as the record of that first
> run; read the 2026-06-23 report for the current validation state.

First sustained real-data run of the pipeline on an operator WSL2 host, following
`docs/runbooks/smoke-run.md`. **Verdict: GREEN** — the spine ran end-to-end on real data
with zero errors; the two zero-valued metrics were investigated and are both
**expected for this seed data**, not defects.

## Setup
- Datasets: `opensanctions=ie_unlawful_organizations`, `geonames=AD` (Andorra).
- Mode: `MERGE_GUARD_MODE=block` (production posture; driver default).
- Egress check: GeoNames reachable; OpenSanctions reachable (the dataset fetched).
- Stack: `deploy/compose.yaml` after the auth + bounded-memory fixes (ADR 0033, 0034).

## Volumes
- **Graph nodes: 3117** (~3113 GeoNames `Address` for Andorra + 4 OpenSanctions: 2
  `Organization` (IRA, INLA) + 2 `Sanction`).
- **Graph edges: 0** — investigated below (expected).
- **Parked merges: 0** — investigated below (expected).
- **Errors: 0.** No `ingest_dead_letter`, no `task_run` errors.
- Driver behaviour: stable; the WS5 resilience kept it running, and after the Neo4j boot
  fix the resolve passes succeeded and auto-drained the queue (no restart needed).

## Investigation 1 — `graph_edges=0` (is edge materialization firing? is H3?)

**Expected for this data. Not a bug; H3 is NOT manifesting.**

- **GeoNames** emits only `Address` with value-only properties (name, country, lat, long)
  — no entity-typed property, so it is structurally edge-free
  (`plugins/connectors/geonames/connector.py:100-113`).
- **OpenSanctions `ie_unlawful_organizations`** is exactly 4 entities: 2 `Organization`
  + 2 `Sanction`. The Sanctions carry a real relationship, `Sanction.entity → org` — but
  its **range is the abstract `Thing` schema**, which ftmg has no node label for, so
  `generate_entity_links` **skips it** (`ftmg/transform.py:227-229`: `srconfig` is `None`
  → continue) **before** the H3 `entity:`-prefix `MATCH` code is ever reached. This is the
  already-documented **G3 / abstract-`Thing`-range** limitation (ADR 0023;
  `tests/integration/test_phase1_acceptance.py:88-90`), not a regression.
- **H3 (the `entity:`-prefix `MATCH` mismatch in `generate_entity_links`) is real but
  latent and was not triggered here.** If H3 were firing, ftmg would still *emit* a link
  query (that then matches nothing) and the write log would show it; the log shows **no
  edge queries**, because ftmg generated none (the only candidate link is dropped at the
  Thing-range check upstream of H3). This was confirmed empirically: running ftmg's
  generators on the 4 real entities yields **0 link queries**.
- The edge path itself works — edge-**schema** entities (Ownership/Directorship) use raw
  endpoint ids and materialise correctly (`graph/writer.py:161`; proven by the deliberate
  Ownership edge in `test_referent_rewriting.py`). This data simply contains none.

**Conclusion:** `0` edges is correct for Andorra addresses + 4 distinct sanctions
entities. The only relationship present (`Sanction → org`) is dropped by the documented
G3 deferral. **To exercise edge materialisation on real data**, seed a dataset with
edge-schema relationships (e.g. a corporate-ownership / `us_*` company dataset). **To
exercise (and expose) H3**, seed data with a *concrete*-range entity link
(e.g. `Person.addressEntity → Address`) — those would silently drop until H3 is fixed
(deferred, ADR 0032 / `ARCHITECTURE_REVIEW.md` H3).

## Investigation 2 — `parked_merges=0` (should this data park anything?)

**Expected for this data. Not a gap.**

- `needs_review` short-circuits on `if not cluster.is_merge: return False`
  (`resolution/review.py:38`): a sensitive **singleton is never parked** — it is promoted
  and written directly (the IRA is written as a node, `test_phase1_acceptance.py:71-94`).
  Parking requires a cluster of **2+ members** that is oversized (>10) **or** has a
  sensitive member.
- The seed data has no such cluster: `ie_unlawful_organizations` is **distinct** orgs
  (the sanctioned IRA/INLA resolve to sensitive singletons → written, not parked); the
  Andorran addresses are non-sensitive (no topics); there is no cross-schema name+country
  collision; and `uq_er_queue_dedup` makes a re-ingest idempotent, so no synthetic
  duplicate cluster forms.
- Block mode **was** live (the driver calls `resolve_pending` with no override →
  settings default `block`, `driver.py:260` / `settings.py:52`), so the guard would have
  parked a flagged cluster — there simply was none.

**Conclusion:** `0` parked merges is correct. **To exercise parking**, seed a **duplicate
of a sensitive entity** (two records of the same sanctioned person/org → a flagged 2+
cluster, as in `test_resolution_pipeline.py:56-103`, which yields `review==1` + a
`pending_review` audit row) or any >10 cluster.

## Verdict

**GREEN.** End-to-end on real data, 0 errors, all invariants exercised on the live stack
(provenance written, tenant-scoped, block-mode guard live, idempotent enqueue). The two
zeros are correct for distinct-orgs + non-sensitive-addresses seed data, not defects. The
G3 (Sanction-link) and H3 (concrete-range-link) drops remain **known, deferred**
limitations that this dataset does not expose; the suggested seed datasets above would
exercise them when those gates are scheduled.
