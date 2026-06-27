# Runbook — driver supervision & containerization

> Gate B-4c · ADR [0051](../decisions/0051-driver-supervision-containerization.md) ·
> spec [`GATE_B4C_DRIVER_SUPERVISION_SPEC.md`](../reviews/GATE_B4C_DRIVER_SUPERVISION_SPEC.md)

Before B-4c the ingest driver was started **by hand** as a foreground host process (see
`smoke-run.md`) — nothing restarted it on reboot / lost shell / OOM-kill, it had no memory cap, and
`/health` was a pure liveness echo that returned `ok` without touching any store. The five stores
could be green, `/health` could say `ok`, and **no ingest/resolve was happening** — the audit's
"false-confidence signal". This gate closes that with three things:

| Surface | What it proves |
|---|---|
| **`/ready`** (API) | a REAL, fail-closed store-reachability probe — `200` iff Postgres + Neo4j + MinIO are all reachable, `503` naming the down store otherwise. Unlike `/health`, it actually reaches the stores. |
| **driver last-tick heartbeat** | a per-container file the driver rewrites once per loop tick; `python -m worldmonitor.runner.driver --healthcheck` reports the pipeline **down** the moment that file goes stale or missing — even while the process still echoes `/health`=ok. |
| **supervised compose services** | `api` + `driver` run from one image with `restart: unless-stopped`, a `mem_limit`, `depends_on: service_healthy`, and the two healthchecks above. |

`/health` is deliberately **UNCHANGED** — see "Liveness vs readiness" below.

---

## The image (one image, two roles)

A single `Dockerfile` at the repo root builds **one** application image used by both services; each
picks its role via `command`:

- **api** — `uvicorn worldmonitor.api.main:create_app --factory --host 0.0.0.0 --port 8000`
- **driver** — `python -m worldmonitor.runner.driver`

It is a multi-stage build (a builder with the C toolchain + ICU headers that PyICU compiles
against; a slim runtime carrying only the shared libs + the prebuilt venv), runs as a **non-root**
user, and bakes in **no secrets** — every value comes from the environment at run time (12-factor).

Build it locally:

```bash
docker compose --env-file .env -f deploy/compose.yaml build api driver
```

---

## Bring up the supervised stack

```bash
cp .env.example .env          # fill in every change-me (incl. a real CONFIG_ENCRYPTION_KEY)
docker compose -f deploy/compose.yaml up -d --wait api driver
```

`up --wait api driver` pulls in their `depends_on` and blocks until they are **healthy**:

1. `postgres`, `neo4j`, `minio` reach `service_healthy`.
2. one-shot `migrate` runs `python -m worldmonitor.db.migrate` (alembic upgrade head) and exits 0 —
   the app schema must exist before the driver's startup `recover_stale()` queries `task_run`.
3. one-shot `minio-init` runs the app's own `LandingStore.ensure_bucket()` and exits 0 — the
   landing bucket must exist or the `/ready` MinIO probe (`head_bucket`) is `503` forever.
4. `api` goes healthy once `/ready` returns `200`; `driver` goes healthy once its heartbeat is fresh.

> **Required Fernet key.** `build_driver` constructs a `ConfigCipher` eagerly, which needs a VALID
> Fernet key in `CONFIG_ENCRYPTION_KEY` (generate: `python -c "from cryptography.fernet import
> Fernet; print(Fernet.generate_key().decode())"`). A bad/empty key crash-loops the driver. The
> `--healthcheck` subcommand itself needs **no** key — it only reads the heartbeat file.

---

## Liveness vs readiness (`/health` vs `/ready`)

This split is deliberate (ADR 0051 D2/D4):

- **`/health`** — a cheap, dependency-free **liveness** echo (`{"status":"ok","environment":...}`).
  It stays liveness-only so an orchestrator never kills the process for a transient downstream blip.
  Unauthenticated. **Unchanged by this gate.**
- **`/ready`** — a **readiness** probe that actually reaches every store, read-only and
  timeout-bounded (`READINESS_PROBE_TIMEOUT_SECONDS`, default 5s, per store):
  - Postgres `SELECT 1`, Neo4j `verify_connectivity()`, MinIO `head_bucket` on the landing bucket.
  - `200` + `{"ready": true, "checks": {"postgres":"ok","neo4j":"ok","minio":"ok"}}` iff all pass.
  - `503` + the per-component body naming the down store(s) otherwise.
  - Unauthenticated (a probe endpoint, like `/health`).

```bash
curl -s http://localhost:8000/health   # always {"status":"ok",...} while the process is up
curl -si http://localhost:8000/ready   # 200 all-ok, or 503 naming the down store
```

The false-confidence distinction the audit demands: with (say) Neo4j down, `/health` still returns
`200 ok` while `/ready` returns `503` with `"neo4j":"down"`.

---

## Check driver liveness

The driver rewrites its heartbeat file (`DRIVER_HEARTBEAT_PATH`, default
`/var/run/worldmonitor/driver.heartbeat`) once per loop tick — **every** tick, even when idle (an
idle driver is still alive). The file is older than `DRIVER_HEARTBEAT_STALE_SECONDS` (default `90`,
≈ 3 missed 30s `DRIVER_TICK_SECONDS` ticks) ⇒ **stale** ⇒ down.

```bash
# From the host: is the supervised driver healthy/unhealthy?
docker compose -f deploy/compose.yaml ps driver

# The exact command the container HEALTHCHECK runs (exit 0 alive / 1 missing-or-stale):
docker compose -f deploy/compose.yaml exec driver \
  python -m worldmonitor.runner.driver --healthcheck; echo "exit=$?"
```

A **stalled** pipeline (the process is up, ticking nothing) shows up here as `unhealthy` /
`--healthcheck` exit `1`, while `/health` still echoes `ok` — exactly the dead-pipeline detection
this gate adds. `--healthcheck` reads **only** the file: it opens no store connections and never
constructs the full driver.

---

## Supervision honesty (what restart does and does NOT cover)

Stated plainly so on-call expectations are correct (ADR 0051 D1):

- `restart: unless-stopped` restarts a service on **process exit** — this covers OOM-kill, crash,
  and host reboot. **This is the core B-4 fix.**
- Plain Compose does **NOT** auto-restart a container that is merely **unhealthy** (e.g. a stalled
  driver whose process is still up). The `HEALTHCHECK` makes a stall **observable**
  (`docker compose ps` → `unhealthy`) and is the hook for a future autoheal sidecar / k8s liveness
  probe. **Auto-restart-on-stall is a named follow-up, not this gate.**
- `mem_limit` (`WM_DRIVER_MEM_LIMIT` / `WM_API_MEM_LIMIT`, default `1g`) caps each container so an
  OOM (e.g. the GeoNames stream, H-6) is bounded to that container and surfaces as an exit →
  restart. It does **not** fix the underlying stream memory use (H-6, out of scope here).

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `api` never reaches healthy; `/ready` → `503 "minio":"down"` | landing bucket missing — confirm the `minio-init` one-shot exited 0 (`docker compose logs minio-init`). |
| `api` → `503 "postgres":"down"` but Postgres is up | wrong `POSTGRES_DSN` host (must be the service name `postgres`, not `localhost`, inside the compose network). |
| `driver` crash-loops at startup | invalid/empty `CONFIG_ENCRYPTION_KEY` (ConfigCipher), or the schema is missing (`migrate` did not complete). Check `docker compose logs migrate driver`. |
| `driver` is `unhealthy` but the process is up | the heartbeat is stale — the loop is stalled (a wedged ingest/resolve). Inspect `docker compose logs driver`. |
| `/ready` hangs | a store is black-holing connections; each probe is bounded by `READINESS_PROBE_TIMEOUT_SECONDS` and then reported `down`. |

CI proves this end-to-end: `.github/workflows/compose-boot.yml` builds the image and runs
`up --wait api driver`, so a healthy `api` proves `/ready`=200 against the real stack and a healthy
`driver` proves the heartbeat is fresh.
