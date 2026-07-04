# 0051 — Driver supervision, containerization & a real readiness surface

- **Status:** ACCEPTED
- **Date:** 2026-06-27
- **Gate:** B-4c — driver supervision & containerization (`docs/reviews/GATE_B4C_DRIVER_SUPERVISION_SPEC.md`). **BUILD gate.**
- **Closes (one of three B-4 defects):** the cross-line audit finding **B-4** bundles three defects:
  (1) no backup/restore — **DONE** in B-4b ([0050](0050-backup-restore-disaster-recovery.md)); (2) the
  ingest driver is **unsupervised and not even in compose** (no Dockerfile, launched by hand as a
  foreground host process — nothing restarts it on reboot / lost shell / OOM-kill); (3) `/health` is a
  pure liveness echo that returns `ok` without touching any store and the driver exposes **no** health
  surface — so the five backing services come back green, `/health` says `ok`, but no ingest and no
  resolution happen and nothing pages. This ADR closes (2) + (3).
- **Defers:** dead-letter (`ingest_dead_letter`) pruning / retention → **B-4d** (ties to finding M-6,
  not this gate). `task_run` pruning already exists (`IngestDriver.prune_task_runs`); the dead-letter
  table has no retention yet — a separate slice.
- **Preserves (does NOT relitigate):** G1 `prov_*` on every node AND edge (`graph/writer.py`);
  [0044](0044-anchor-preferred-stable-ids.md)/[0048](0048-ftm-valid-injective-durable-id.md) durable
  ids + the `canonical_id_ledger` (never touched — no un-merge); [0029](0029-ingest-driver.md) driver
  cadence/serialization/startup-recovery semantics (unchanged); [0030](0030-alembic-migrations.md)
  migration drift guard; `DEFAULT_MERGE_THRESHOLD=0.92` + Splink + `cluster_and_merge` (untouched).
- **Touches:** `Dockerfile` (NEW), `.dockerignore` (NEW), `deploy/compose.yaml` (+`api` + `driver`
  services), `src/worldmonitor/api/main.py` (+`/ready`), `src/worldmonitor/api/readiness.py` (NEW),
  `src/worldmonitor/runner/heartbeat.py` (NEW), `src/worldmonitor/runner/driver.py` (heartbeat write +
  `--healthcheck` subcommand), `settings.py` (+heartbeat/probe knobs), `.env.example`,
  `.github/workflows/compose-boot.yml` (wait on `api`+`driver`). **No migration**; `db/models.py`
  read-only and NOT touched.
- **Independently built on the other line:** Workflow A supervises its driver in compose with a
  container healthcheck. B re-derives against B's code (the `IngestDriver` loop, the existing store
  clients, the existing single-pinned-image build) — does **not** copy.

## Context

`deploy/compose.yaml` defines only `postgres / neo4j / minio / redis / zitadel` (all
`restart: unless-stopped`). There is **no worldmonitor app/driver service and no Dockerfile**. The
driver is the long-running spine (ADR 0029): it reads the `ConnectorInstance` registry, runs each
enabled connector on a cadence, and resolves the ER queue on an independent cadence. Today it is
launched by hand per `docs/runbooks/smoke-run.md` as a foreground host process. Nothing restarts it on
host reboot, a lost shell, or an OOM-kill (very reachable — GeoNames loads a whole country zip into RAM,
finding H-6, and the driver has **no `mem_limit`** — only Neo4j does).

`api/main.py:48` `/health` returns `{"status":"ok","environment":...}` without touching any store — a
pure liveness echo. The driver exposes no health/metrics surface at all. `tests/unit/test_api_health.py`
asserts `/health`=ok — which the audit calls out as *"exactly the false-confidence signal."* A dead
pipeline is indistinguishable from a live one.

This is **ops hardening, not a product/architecture fork** and **not person-affecting** (it merges
nothing, scores nothing, mutates no threshold, touches no real-person record). No OPEN question — the
design is determinable from CLAUDE.md (12-factor, containerized, single-node, `asyncio` + task table)
+ the existing code. `human_fork: false`.

## Decision

Make the driver a supervised container with a meaningful healthcheck, and split health into the
standard **liveness vs readiness** pair.

### D1 — One application image; two compose services (same image, two commands)

A single `Dockerfile` (slim Python 3.12, `uv`-installed package) + a `.dockerignore`. `deploy/compose.yaml`
gains **two** services built from that one image:

- **`driver`** — `command: python -m worldmonitor.runner.driver`. `restart: unless-stopped`,
  `depends_on: {postgres, neo4j, minio: service_healthy}`, a `mem_limit` (`${WM_DRIVER_MEM_LIMIT:-1g}`)
  so an OOM is bounded to the driver container and surfaces as a process exit, and a `HEALTHCHECK` that
  runs `python -m worldmonitor.runner.driver --healthcheck` (D3).
- **`api`** — `command: uvicorn worldmonitor.api.main:create_app --factory --host 0.0.0.0 --port 8000`.
  `restart: unless-stopped`, the same `depends_on`, a `mem_limit`, and a `HEALTHCHECK` that curls
  `/ready` (D2). Adding the API service is what gives `/ready` a real deployment and a real container
  healthcheck; it is one extra stanza over the same image, not a new build.

**Supervision honesty (stated, not hidden):** in plain Docker/Compose, `restart: unless-stopped`
restarts a container on **process exit/crash** (so an OOM-kill or a host reboot **is** covered — the
core B-4 fix), but Compose does **not** auto-restart a container merely because its `HEALTHCHECK`
reports *unhealthy* (that is Swarm/k8s behavior). So the healthcheck's job here is twofold and bounded:
(a) `depends_on: service_healthy` ordering, and (b) making a *stalled-but-not-exited* driver
**observable** (`docker compose ps` shows `unhealthy`) and giving a future k8s liveness probe / an
`autoheal` sidecar a hook. Auto-restart-on-stall is a **named follow-up**, not this gate.

### D2 — `/ready`: real, fail-closed, per-store reachability (distinct from `/health`)

`/health` stays exactly as it is — an unauthenticated **liveness** echo (the process is up). We do
**not** change it (the FROZEN `test_api_health.py` stays green); k8s/Compose model liveness and
readiness as two separate probes and so do we.

`src/worldmonitor/api/readiness.py` adds `check_readiness(...)` that probes each store with a
**read-only, bounded** call:

- **Postgres** — `SELECT 1` on a short-lived session.
- **Neo4j** — `Neo4jClient.verify()` (driver `verify_connectivity`).
- **MinIO** — a read-only `head_bucket` on the landing bucket (via the existing client; **no** write,
  **no** `ensure_bucket`).

`check_readiness` takes the probes/clients as **injected dependencies** (callables/clients), so it is
unit-testable with fakes and never needs a live stack to test the logic. `/ready` returns **200** with
a per-component body `{"ready": true, "checks": {"postgres": "ok", "neo4j": "ok", "minio": "ok"}}`
**iff every** probe succeeds, else **503** with the failing component(s) named. Unauthenticated (a probe
endpoint, like `/health`). Each probe is wrapped so one store being down reports `"down"` for that
component (and 503 overall) rather than throwing a 500.

### D3 — Driver liveness via a file-based last-tick heartbeat + a `--healthcheck` subcommand

`src/worldmonitor/runner/heartbeat.py` owns a tiny, pure, unit-testable heartbeat:

- `Heartbeat(path, stale_after_seconds)` with `touch(now)` (atomic write — temp file + `os.replace`)
  and `is_alive(now) -> bool` (file exists **and** `now - last_tick <= stale_after_seconds`).
- `IngestDriver.run_forever` calls `heartbeat.touch(now)` **once per loop iteration** (every tick,
  whether or not there was ingest/resolve work — an *idle* driver is alive). The single `touch` call
  sits in the existing `# pragma: no cover` loop; **all** of the read/compare/staleness logic lives in
  `heartbeat.py` and is fully covered.
- `python -m worldmonitor.runner.driver --healthcheck` reads the heartbeat and **exits 0** if alive,
  **non-zero** if the file is missing or stale. This is the driver container's `HEALTHCHECK` command —
  the same code path the unit test asserts.

The heartbeat path defaults to a per-container file (`settings.driver_heartbeat_path`, e.g.
`/var/run/worldmonitor/driver.heartbeat`); staleness defaults to a safe multiple of
`driver_tick_seconds` (`settings.driver_heartbeat_stale_seconds`).

**The load-bearing invariant this proves:** a stale heartbeat (last-tick timestamp old) makes the
liveness check report the pipeline **down** even though the process echoes `/health`=ok — the exact
false-confidence the audit flags, now testable in-process.

## Alternatives considered

- **(A) Heartbeat in a new Postgres `driver_heartbeat` table, read by `/ready`.** Rejected for this
  gate. It would (i) require a **migration** + a `db/models.py` edit (a large, heavily-guarded blast
  radius for what is a per-container liveness signal), and (ii) **conflate** the API's *readiness to
  serve* with the *driver's* liveness — two different processes' concerns on one endpoint. The
  conventional supervision primitive is a **per-container healthcheck**, which is exactly what
  `restart`/`depends_on` and a future k8s liveness probe act on. A shared durable heartbeat row that
  `/ready` also surfaces is a reasonable **future enhancement** (a single operational pane), but it is
  not needed to kill the false-confidence and it is not worth a migration here.
- **(B) Heartbeat in Redis (key + TTL).** Rejected — Redis is in compose but **not yet wired in any
  application code**; introducing a Redis client dependency for a heartbeat is a larger, unrelated
  blast radius. TTL-as-staleness is elegant but does not justify a new store-client now.
- **(C) Heartbeat file on a shared compose volume read by both `api` and `driver`.** Rejected — a
  cross-container shared *writable* runtime volume is fragile (and breaks the moment the two processes
  land on different hosts). Unnecessary: the driver checks **its own** file in **its own** container;
  the API does not need to read it (D2 keeps `/ready` to store reachability).
- **(D) Make `/health` itself probe the stores.** Rejected — that destroys the liveness/readiness
  distinction (a liveness probe must stay cheap and dependency-free so an orchestrator does not kill a
  pod for a transient downstream blip). We **add** `/ready`; `/health` stays a liveness echo (and its
  test stays frozen).
- **(E) Fold dead-letter pruning into this gate.** Rejected — separate concern (retention/M-6), its
  own slice **B-4d**. Keep this gate small and coherent around supervision + health.
- **(F) Auto-restart the driver on *unhealthy* (autoheal sidecar / Swarm / k8s).** Rejected for this
  gate — `restart: unless-stopped` already covers the audit's crash/OOM/reboot scenario (process exit);
  restart-on-stall is a named follow-up requiring an orchestration choice out of scope here.

## Consequences

- The driver is supervised: a crash / OOM-kill / host reboot restarts it (`restart: unless-stopped`),
  its memory is bounded (`mem_limit`), it starts only after its stores are healthy
  (`depends_on: service_healthy`), and a stall is observable (`HEALTHCHECK` → `unhealthy`).
- `/ready` distinguishes a serve-ready API (all stores reachable) from a degraded one; `/health`
  stays a cheap liveness echo.
- A dead/stalled pipeline is detectable — the heartbeat `--healthcheck` reports down while `/health`
  still echoes ok. The false-confidence signal is closed.
- **No migration**, no new table, no `db/models.py` change, no Redis wiring, no pipeline/merge/writer/
  guard change. The Dockerfile + compose services are verified by the **existing** `compose-boot` CI
  job (extended to `up --wait` the `api` + `driver` services; no new CI job). `compose-boot` must
  generate a valid Fernet `CONFIG_ENCRYPTION_KEY` for the driver to boot (see spec §6).

## Out of scope (hard stops)

Dead-letter pruning/retention (B-4d); a Postgres/Redis heartbeat store + any migration; changing
`/health` semantics or removing its test; multi-replica lease / HA (ADR 0029 fork X2); an autoheal
sidecar / Swarm / k8s liveness auto-restart; the GeoNames streaming fix (H-6 — `mem_limit` only bounds
the blast radius here); any change to ingest/resolve cadence, serialization, startup recovery, or any
`pipeline.py` / `merge.py` / `writer.py` / resolver / guard code; any new CI job.
</content>
</invoke>
