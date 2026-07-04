# Runbook — sustained real-data smoke run (block mode)

> **Audience:** an operator with a Docker host. This is run **out-of-band on your own
> stack** — the build environment has no Docker and filtered egress, so it cannot run
> this. Goal: drive the ingest pipeline against **real** OpenSanctions + GeoNames data
> over a sustained cadence, in **block mode** (production posture, ADR 0031), and capture
> how it behaves over time.

## What this exercises
The full spine on a timer (ADR 0029): `ConnectorInstance` registry → `run_ingest`
(land → map → enqueue, windowed/bounded/dead-lettered) → `resolve_pending` (batch
clustering → catastrophic-merge guard → referent rewrite → graph write), with the guard
in **block** mode so sensitive (PEP/sanctioned) merges **park** for sign-off instead of
auto-writing.

## 1. Prerequisites

```bash
# From the repo root, on a host WITH Docker:
docker compose -f deploy/compose.yaml up -d        # postgres, neo4j, minio, redis, zitadel
uv sync                                             # install the package + deps

# Environment (a .env or exported vars). Defaults assume the compose stack:
export POSTGRES_DSN="postgresql://worldmonitor:worldmonitor@localhost:5432/worldmonitor"
export NEO4J_URI="bolt://localhost:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="<your compose neo4j password>"
export MINIO_ENDPOINT="localhost:9000"
export MINIO_ACCESS_KEY="worldmonitor"
export MINIO_SECRET_KEY="<your compose minio secret>"
export LANDING_BUCKET="landing"
export CONFIG_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export MERGE_GUARD_MODE="block"                     # default already; set explicitly to be sure
# Cadence — tighten for a smoke run so you see resolution often:
export INGEST_CADENCE_SECONDS=3600                  # re-ingest each connector hourly
export RESOLVE_CADENCE_SECONDS=120                  # resolve the queue every 2 min
export DRIVER_TICK_SECONDS=15
# Resolve the whole richer seed (~2.7k entities) in ONE batch so the OFAC collision
# pair lands together — a merge only forms WITHIN a single batch (batch-first, ADR 0026),
# so a value below the seed size could split the pair and silently skip the park.
export RESOLVE_BATCH_SIZE=5000

# Create the schema (Alembic, ADR 0030) and the landing bucket:
python -m worldmonitor.db.migrate                   # alembic upgrade head
# Create the MinIO bucket "landing" via the console (http://localhost:9001) or mc.
```

Confirm egress: the build env saw `data.opensanctions.org` return **403** — verify from
*your* host that both sources are reachable before a long run:

```bash
curl -sI https://data.opensanctions.org/datasets/latest/us_dod_chinese_milcorps/entities.ftm.json | head -1
curl -sI https://data.opensanctions.org/datasets/latest/us_ofac_sdn/entities.ftm.json | head -1
```

## 2. Seed connector instances (enabled)

Configs are Fernet-encrypted at rest (ADR: decrypt-at-use). This **richer seed** exercises
the **edge** and **parking** paths on real data — the minimal `ie_unlawful_organizations`
+ Andorra seed proved only the node path (see `smoke-run-report-2026-06-22.md`):

```python
# python - <<'PY'   (run with the env above exported)
import json, uuid
from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.db.models import ConnectorInstance
from worldmonitor.settings import get_settings

settings = get_settings()
cipher = ConfigCipher.from_settings(settings)
sessions = session_factory(engine_from_settings(settings))

instances = [
    # EDGES (+ G3): DoD "Chinese Military Companies" — 208 Company + 130 Ownership edges
    # (every endpoint present -> real Company-OWNS->Company relationships). NO 'limit':
    # its last edge endpoint sits at stream position ~733/735, so any cap orphans edges.
    # It also carries 397 Sanction entities whose Sanction.entity links are DROPPED by the
    # known G3 abstract-Thing-range limit (surface in the report; do NOT fix G3 here).
    ("opensanctions", {"dataset": "us_dod_chinese_milcorps"}),
    # PARKING: OFAC SDN, first 2000 entities — contains the real name-collision pair
    # "OOO Legion Komplekt" (two DISTINCT sanctioned ids, identical name + country=ru, no
    # birthDate) that Splink fuses at 0.9825 >= 0.92 -> a sensitive merge that block mode
    # PARKS. (limit=2000 is required to bound the 70k-entity dataset to a smoke size while
    # still capturing both members of the pair, which sit within the first ~1950 lines.)
    ("opensanctions", {"dataset": "us_ofac_sdn", "limit": 2000}),
]
with sessions() as s:
    for connector_id, config in instances:
        s.add(ConnectorInstance(
            id=str(uuid.uuid4()), connector_id=connector_id,
            config_encrypted=cipher.encrypt(json.dumps(config)), status="enabled",
        ))
    s.commit()
print("seeded", len(instances), "instances")
# PY
```

> The driver **refuses `ACTIVE`-capability** connectors visibly (a `task_run` error) —
> both are passive `EXTERNAL_IMPORT`, so they run.
> The Andorra/GeoNames node baseline is intentionally dropped here: 3000 same-country
> addresses in one batch bloat Splink's blocking group and would slow the focused run;
> re-add `("geonames", {"country": "AD"})` only if you want the multi-source node breadth.

## 3. Launch the driver

```bash
python -m worldmonitor.runner.driver           # runs forever; Ctrl-C to stop
# (capture logs + RSS:)
#   python -m worldmonitor.runner.driver 2>&1 | tee smoke-driver.log &
#   while kill -0 $! 2>/dev/null; do ps -o rss= -p $! | awk '{print strftime("%H:%M:%S"), $1"KB"}'; sleep 60; done
```

It logs its `guard_mode` and cadences on start. On startup it also **resets stale
`running` rows** from any prior crash (single-node recovery).

## 4. Monitor

```bash
watch -n 30 python -m worldmonitor.runner.smoke_metrics
```

Each snapshot prints (read-only, Postgres + graph):

| metric | meaning |
|---|---|
| `queue_pending` | ER-queue backlog; should rise on ingest, fall to 0 after a resolve cadence |
| `queue_pending_review` | rows parked for sign-off (block mode) |
| `parked_merges` | `merge_audit` rows awaiting sign-off — **block-mode parking firing** |
| `dead_letter` | `ingest_dead_letter` rows — land/map failures (should stay low/zero) |
| `task_ingest_ok/error/running` | ingest run outcomes |
| `task_resolve_ok/error/running` | resolution run outcomes |
| `graph_nodes` / `graph_edges` | resolved-graph size (should grow, then plateau) |

Review the parked merges and exercise sign-off (ADR 0031):

```bash
python -m worldmonitor.review list
python -m worldmonitor.review approve --canonical NK-... --approver you
python -m worldmonitor.review reject  --canonical NK-... --approver you
```

## 5. Success / failure criteria

**Success:**
- `task_ingest_ok` increments per connector; `task_*_error` stays 0.
- `queue_pending` drains to 0 within a resolve cadence after each ingest.
- `graph_nodes` grow then plateau on re-ingest (idempotent MERGE — no unbounded
  duplication within a run).
- **`graph_edges >= 130`** — the DoD `Ownership` edges materialize as real
  `Company-OWNS->Company` relationships. This is the edge path on real data; `0` would mean
  edge materialization is not firing (the minimal seed produced `0` because it had no
  edge-schema entities).
- **`parked_merges >= 1`** — block mode parks the OFAC "OOO Legion Komplekt" collision (a
  sensitive merge); `python -m worldmonitor.review list` shows it, and
  `approve`/`reject` works. (The minimal seed produced `0`; this is the path it didn't cover.)
- **G3 (expected, NOT to fix here):** the ~397 DoD `Sanction.entity` links + the OFAC
  sanctions do **not** become graph edges — they hit the documented abstract-`Thing`-range
  drop (ADR 0023, `ARCHITECTURE_REVIEW.md` G3). Count them (Sanction entities written as
  nodes with no outgoing edge) and note it in the report; **do not fix G3** — that is a
  separate decision.
- `dead_letter` stays at 0 (or a small, explained count).
- No `task_*_running` row stuck for longer than a run should take.

**Failure / investigate:**
- `task_*_error` climbing → read the `task_run.error` summaries.
- `dead_letter` climbing → land/map failures; inspect `ingest_dead_letter.stage` (raw is
  still in the landing zone — replayable).
- `queue_pending` not draining → resolution stalled or erroring.
- RSS growing unbounded over hours → a leak (note the dataset + rate).
- A driver crash → on restart, stale `running` rows are reset; confirm no double-enqueue
  (idempotent enqueue, ADR 0029).

## 6. What to watch over a sustained run
- **Source rate-limits:** HTTP 429 / connection resets in the driver log (OpenSanctions
  and GeoNames are public; back off the cadence if throttled).
- **Memory growth:** RSS trend of the driver process over hours (the loop is bounded per
  window, so RSS should be flat).
- **Dead-letter accumulation:** any steady `ingest_dead_letter` growth is a mapping/source
  defect to file.
- **Block-mode parking:** `parked_merges` should grow with sanctioned data and be
  drainable via the review CLI — confirm it does not silently auto-write a sensitive merge.

## 7. Report template

```markdown
# Smoke-run report — <date>, <host>
- Seed: us_dod_chinese_milcorps (edges) + us_ofac_sdn limit=2000 (parking);
  duration: <hh:mm>; cadences: <...>; RESOLVE_BATCH_SIZE=<n>
- Egress check: us_dod_chinese_milcorps=<code>, us_ofac_sdn=<code>

## Volumes
- ingested (records landed): <n>   resolved (clusters/promoted): <n>
- graph: nodes=<n> edges=<n>   parked_merges=<n>   dead_letter=<n>

## Path coverage (the point of the richer run)
- EDGES: graph_edges=<n> (expect >= 130 Company-OWNS->Company); `review`-confirmed a sample
  Ownership lands on both Company nodes? <yes/no>
- PARKING: parked_merges=<n> (expect >= 1); `review list` shows the OFAC
  "OOO Legion Komplekt" pair? <yes/no>; approve/reject exercised? <yes/no>
- G3 (surface, do NOT fix): Sanction entities written=<n>, Sanction.entity edges in graph=0
  (the documented Thing-range drop on real data)

## Driver behavior over time
- RSS: start=<KB> end=<KB> trend=<flat/growing>
- task_run: ingest ok/err=<n>/<n>  resolve ok/err=<n>/<n>
- rate-limits observed: <none / 429s at ...>

## Bugs found
- <id> <one-line> — <what / where> → fix via the build→review→merge flow.

## Verdict
- <pass / pass-with-bugs / fail> — <one line>
```
