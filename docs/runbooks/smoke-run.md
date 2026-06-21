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

# Create the schema (Alembic, ADR 0030) and the landing bucket:
python -m worldmonitor.db.migrate                   # alembic upgrade head
# Create the MinIO bucket "landing" via the console (http://localhost:9001) or mc.
```

Confirm egress: the build env saw `data.opensanctions.org` return **403** — verify from
*your* host that both sources are reachable before a long run:

```bash
curl -sI https://data.opensanctions.org/datasets/latest/sanctions/entities.ftm.json | head -1
curl -sI https://download.geonames.org/export/dump/AD.zip | head -1
```

## 2. Seed connector instances (enabled)

Configs are Fernet-encrypted at rest (ADR: decrypt-at-use). Seed two **enabled**
instances — start with small datasets to validate, then point at larger ones for volume:

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

TENANT = "smoke"
instances = [
    # OpenSanctions: 'sanctions' / 'peps' give heavy PEP/sanctioned data -> block-mode
    # parking fires. Start with the tiny 'ie_unlawful_organizations' to validate.
    ("opensanctions", {"dataset": "ie_unlawful_organizations"}),
    # GeoNames: a small country (AD=Andorra) to validate; a big one (e.g. US) for volume.
    ("geonames", {"country": "AD"}),
]
with sessions() as s:
    for connector_id, config in instances:
        s.add(ConnectorInstance(
            id=str(uuid.uuid4()), tenant_id=TENANT, connector_id=connector_id,
            config_encrypted=cipher.encrypt(json.dumps(config)), status="enabled",
        ))
    s.commit()
print("seeded", len(instances), "instances for tenant", TENANT)
# PY
```

> The driver **refuses `ACTIVE`-capability** connectors visibly (a `task_run` error) —
> both of these are passive `EXTERNAL_IMPORT`, so they run.

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
python -m worldmonitor.review list    --tenant smoke
python -m worldmonitor.review approve --tenant smoke --canonical NK-... --approver you
python -m worldmonitor.review reject  --tenant smoke --canonical NK-... --approver you
```

## 5. Success / failure criteria

**Success:**
- `task_ingest_ok` increments per connector; `task_*_error` stays 0.
- `queue_pending` drains to 0 within a resolve cadence after each ingest.
- `graph_nodes`/`graph_edges` grow then plateau on re-ingest (idempotent MERGE — no
  unbounded duplication within a run).
- For OpenSanctions PEP/sanctioned data, `parked_merges` > 0 — block mode is parking
  sensitive merges (expected and correct); sign-off `approve`/`reject` works.
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
- Datasets: opensanctions=<dataset>, geonames=<country>; duration: <hh:mm>; cadences: <...>
- Egress check: opensanctions=<code>, geonames=<code>

## Volumes
- ingested (records landed): <n>   resolved (clusters/promoted): <n>
- graph: nodes=<n> edges=<n>   parked_merges=<n>   dead_letter=<n>

## Driver behavior over time
- RSS: start=<KB> end=<KB> trend=<flat/growing>
- task_run: ingest ok/err=<n>/<n>  resolve ok/err=<n>/<n>
- rate-limits observed: <none / 429s at ...>

## Bugs found
- <id> <one-line> — <what / where> → fix via the build→review→merge flow.

## Verdict
- <pass / pass-with-bugs / fail> — <one line>
```
