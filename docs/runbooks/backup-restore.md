# Runbook — backup / restore disaster recovery

> Gate B-4b · ADR [0050](../decisions/0050-backup-restore-disaster-recovery.md) ·
> spec [`GATE_B4B_BACKUP_RESTORE_SPEC.md`](../reviews/GATE_B4B_BACKUP_RESTORE_SPEC.md)

WorldMonitor's system of record spans **three stores**, and this is how you back all three up and
recover them after a disk loss:

| Store | What lives there |
|---|---|
| **Postgres** | the H-1 `resolver_judgement` negatives/positives, `sign_off`, `merge_audit`, the no-un-merge `canonical_id_ledger`, `er_queue_item`, `ingest_dead_letter`, `task_run`, `merge_alerts`, `er_gold_pair`, `connector_instance`, and the `alembic_version` row |
| **Neo4j** | the resolved entity graph — the product — with `prov_*` G1 provenance on every node **and** edge, and topic labels |
| **MinIO** | the raw bytes behind every `source_record` provenance pointer (the landing zone) |

One command backs up all three; one command restores all three. Both are a single Python entrypoint
(`python -m worldmonitor.backup`) with thin shell wrappers; ops and the test suite share one code
path. Backup is **read-only** on the live system; restore is **destructive**.

---

## The DR guarantee (why this exists)

A restore that silently dropped the `resolver_judgement` **negatives** would re-enable the exact
**H-1 transitive re-merge** corruption ADR 0037 closes — a bridging record re-fusing a
human-rejected pair into one canonical node — now reached *via disaster recovery*. So the bar is not
"the process exits 0"; it is:

- **H-1 survives restore** — a previously-REJECTED pair is present after restore **and** a fresh
  resolve over the restored judgements does **not** re-fuse it.
- **Canonical-id byte-identity** — the restored node-id set, edge count, and `canonical_id_ledger`
  equal the backup byte-for-byte (esp. the `wm-anchor-qid-*` anchor ids — ADR 0048).
- **G1 provenance** — `prov_*` survives on every restored node **and** edge.
- **Halt-loud** — a partial/corrupt backup can never be silently restored or silently produced.

These are pinned by `tests/integration/test_backup_restore_roundtrip.py` (it seeds an anchored merge
+ a human rejection, backs up, **wipes all three stores**, restores, and asserts the above).

---

## Back up

Run in a **low-activity window** (see the caveat below) and write to a timestamped directory:

```bash
deploy/backup/backup.sh /var/backups/worldmonitor/$(date -u +%Y%m%dT%H%M%SZ)
```

What it does, in order, and **halt-loud** at each step:

1. **Postgres** → `postgres.json` — a SQLAlchemy logical dump of every table + the `alembic_version`
   row (lossless: all PKs are `String`, one DB, JSONB round-trips as JSON).
2. **Neo4j** → `neo4j.json` — an **online** Cypher logical export of nodes (labels + properties incl.
   `id` / `name` / every `prov_*` / `prov_witnesses`) and edges (type + endpoint `id`s + edge
   `prov_*`). No neo4j stop, no outage.
3. **MinIO** → `landing/<key>` + `landing_index.json` — a byte mirror of every landing object (paged
   past the 1000-key cap). An empty bucket writes an **explicit** empty `landing_index.json` (`[]`),
   never a missing file.
4. **`manifest.json`** is written **last**, with `"complete": true`, **only after** every artifact is
   present and its per-store count is re-verified.

If any store export fails, the command **raises** and **no `complete:true` manifest is written** —
so you can never `down -v` believing a broken backup succeeded. A directory whose `manifest.json` is
absent or `"complete": false` is **not** a usable backup.

A sample cron (daily, 02:00 UTC; PITR / incrementals are out of scope):

```cron
0 2 * * *  /opt/worldmonitor/deploy/backup/backup.sh /var/backups/worldmonitor/$(date -u +\%Y\%m\%dT\%H\%M\%SZ) >> /var/log/wm-backup.log 2>&1
```

### Low-activity-window caveat (online Cypher export)

Single-node Neo4j **Community has no online *physical* hot backup** (that is Enterprise). The Neo4j
export here is an **online logical Cypher read** — fully supported, no outage — but it is therefore
**not point-in-time consistent under concurrent writes**. Run it when the graph is quiet: the
resolve cadence (`RESOLVE_CADENCE_SECONDS`) is the only writer in v0, so pause the ingest driver (or
schedule the backup off the resolve cadence) for a consistent snapshot. This trades A's *hard
outage* (its offline `neo4j-admin` dump stops neo4j) for a **quiet-window** recommendation.

> Neo4j **internal element ids** and indexes are **not** in the export — nothing references them
> (the graph keys on the `id` property, ADR 0042), and constraints are re-established idempotently on
> restore. An offline `neo4j-admin` physical dump + a host volume snapshot remain a fine *optional
> bonus* (faster for very large graphs), but are out of scope for this gate.

---

## Restore (disaster recovery)

> **Destructive.** Restore **wipes and rebuilds** all three stores. It is a deliberate operator
> action, **never** agent-auto-run.

**Precondition:** the target stores must already exist and Postgres must be **migrated to head**
(`alembic_version` table present) — restore TRUNCATEs + reloads existing tables and re-stamps
`alembic_version`; it does not create the schema. On a brand-new host: bring the stack up, run the
migrations (`migrate_to_head` / `alembic upgrade head`), then restore.

```bash
# bring the stores up + migrated first, then:
deploy/backup/restore.sh /var/backups/worldmonitor/20260626T000000Z
```

What it does — **all-or-nothing + halt-loud**:

1. **Validate before touching anything** — `manifest.json` exists with `"complete": true`, and
   `postgres.json` / `neo4j.json` / `landing_index.json` are all present and parseable. Any miss →
   **raise, nothing touched** (a half-wipe followed by discovering a bad backup is total loss).
2. **Postgres** — TRUNCATE every table, bulk-insert every row, re-stamp `alembic_version`, staged in
   a **single transaction** committed only **after** Neo4j + MinIO also succeed (a downstream
   failure rolls the entire relational restore back). A post-restore `migrate_to_head` is then a
   no-op.
3. **Neo4j** — `DETACH DELETE` all, re-establish constraints (idempotent), MERGE nodes (grouped by
   label-set) and re-create edges (grouped by type), labels/types inlined APOC-free from the closed
   FtM-schema/topic vocabulary. The application `id` (the canonical id) round-trips byte-for-byte.
4. **MinIO** — recreate + clear the bucket, re-`put` every backed-up object.
5. **Re-verify** — re-count all three stores against the manifest; any mismatch **raises**. A
   half-restore can never return success.

After restore, confirm:

```bash
# a previously-REJECTED pair is still present (H-1 survived DR)
psql "$POSTGRES_DSN" -c "SELECT left_id,right_id,judgement FROM resolver_judgement WHERE judgement='negative';"
# the canonical-id ledger row count matches the backup manifest's postgres_row_counts
psql "$POSTGRES_DSN" -c "SELECT count(*) FROM canonical_id_ledger;"
# alembic is at head (migrate_to_head is now a no-op)
```

---

## H-1 / DR guarantee in one line

A previously human-**rejected** identity pair stays rejected after a full wipe + restore (the row is
present **and** a post-DR resolve does not re-fuse it), and every canonical id + the
`canonical_id_ledger` round-trip byte-for-byte — because restore **reproduces** the durable
relational + graph state point-in-time and never re-runs the resolver, re-clusters, un-merges, or
re-scores anything.

---

## Out of scope (named follow-ups)

- Zitadel auth-DB backup (Phase-2 auth; single-tenant, config-reconstructable).
- Offline `neo4j-admin` physical dump / host volume snapshot / `system`-DB / index export.
- Incremental / point-in-time-recovery / a scheduling daemon (the cron above is the sample).
- A live API/MCP restore surface (Phase 2).
