# Gate B-4c — Driver supervision & containerization

- **Gate:** B-4c (ONE B-4 slice — supervise + containerize the driver; add a real `/ready` + a driver
  last-tick heartbeat). Backup/restore was B-4b; dead-letter pruning is B-4d.
- **Branch:** `gate/b4c-driver-supervision` (off `master` @ `f823fd8`; clean except a docs-only ADR
  0050 status flip already staged).
- **ADR:** `docs/decisions/0051-driver-supervision-containerization.md` (PROPOSED).
- **Severity:** BLOCKER (B-4 is a BLOCKER finding). This slice closes two of B-4's three defects:
  the unsupervised, un-containerized driver, and `/health` that cannot detect a dead pipeline.
- **Independently built on the other line:** Workflow A supervises its driver in compose with a
  container healthcheck. B re-derives on B's terms — does **not** copy.
- **NOT in this gate (other B-4 / related slices):** backup/restore (DONE, B-4b/ADR 0050), dead-letter
  pruning/retention (B-4d), the GeoNames streaming fix (H-6), the multi-replica HA lease (ADR 0029 fork
  X2). Hard stops (§11).

---

## 1. The gap (verified against B's code)

B-4 bundles three defects. B-4b closed (1). This gate closes (2) + (3):

| # | Defect | Evidence in B |
|---|---|---|
| 2 | The driver is **unsupervised and not in compose** | `deploy/compose.yaml` defines only `postgres/neo4j/minio/redis/zitadel`; **no app/driver service, no `Dockerfile`** (none in repo). The driver is launched by hand (`docs/runbooks/smoke-run.md:106-113`) as a foreground host process — nothing restarts it on reboot / lost shell / OOM-kill. It has **no `mem_limit`** (only Neo4j does) while GeoNames can OOM it (H-6). |
| 3 | `/health` **cannot detect a dead pipeline** | `api/main.py:48-51` returns `{"status":"ok",...}` without touching any store — a pure liveness echo. The driver exposes **no** health surface. `tests/unit/test_api_health.py:27-31` asserts `/health`=ok — *"exactly the false-confidence signal"* (audit). The five stores come back green, `/health` says ok, `smoke_metrics` prints — but no ingest/resolve happens and nothing pages. |

**Why these two are ONE gate (the gate-boundary decision).** The healthcheck/heartbeat and the
supervised compose service are **load-bearing for each other**: `depends_on: service_healthy` and a
restart/observability policy are only meaningful once there is a real healthcheck to gate on, and a
healthcheck is only useful once the driver is a supervised container. A real `/ready` + a driver
last-tick heartbeat are precisely the signals that make the compose supervision honest. Bundling them
is one coherent vertical slice of B-4. **Dead-letter pruning** (the audit's "retention" concern) is a
*different* axis (it ties to M-6, mutates `ingest_dead_letter` on a schedule, and has its own
correctness questions) — it is deferred to **B-4d**. This boundary is recorded in ADR 0051.

---

## 2. The load-bearing testable invariant

The audit's sharpest point: a test asserts `/health`==ok, which is the false-confidence signal — the
driver can be **dead** while everything reports **green**. The gate's PRIMARY (failing-test-first)
proof is that the NEW surface **distinguishes a live pipeline from a dead/stalled one**:

1. `/ready` returns **NOT-ready (503)** when a store is unreachable, and **ready (200)** when all three
   stores are reachable — proving `/ready` is a *real* probe, unlike the `/health` echo.
2. The driver **last-tick heartbeat** goes **stale** → the liveness check (`is_alive` /
   `driver --healthcheck`) reports the pipeline **down**, even though the process still echoes
   `/health`=ok.

Everything else in the gate (the `Dockerfile`, the compose `api`/`driver` services, `mem_limit`,
`restart`, `depends_on`) is verified by the **existing `compose-boot` CI job**, not by new unit tests.

### 2.1 Unit/integration-testable in-process vs compose-boot-verified

| Concern | How it is proven |
|---|---|
| `/ready` 200-when-up / 503-when-a-store-down, per-component body | **unit** (`tests/unit/test_api_ready.py`, FastAPI `TestClient` + injected fake probes) |
| heartbeat `is_alive` staleness logic; `--healthcheck` exit codes; missing-file = down | **unit** (`tests/unit/test_driver_heartbeat.py`) |
| (optional) `/ready` + heartbeat against real stores | **integration** (`tests/integration/test_ready_probes.py`, testcontainers) — optional hardening |
| `Dockerfile` builds; `api`+`driver` boot, pass healthchecks, start only after stores healthy | **compose-boot** CI (extend `up --wait` to `api driver`; no new job) |
| `mem_limit` / `restart: unless-stopped` present and valid | **compose-boot** `config` + `ps` (interpolation + presence) |

---

## 3. Design — three pieces, smallest correct footprint

### 3.1 `/health` is UNCHANGED — liveness vs readiness (the deliberate split)

`/health` stays an unauthenticated **liveness** echo (`{"status":"ok","environment":...}`). We do
**not** make it probe stores: a liveness probe must stay cheap and dependency-free so an orchestrator
never kills a process for a transient downstream blip. `tests/unit/test_api_health.py` is **FROZEN**
(keep-green). The fix is the NEW `/ready` + the heartbeat, not a change to `/health` (ADR 0051 D2/D4).

### 3.2 `/ready` — real, fail-closed, per-store reachability

`src/worldmonitor/api/readiness.py`:

- `check_readiness(*, postgres_probe, neo4j_probe, minio_probe) -> ReadinessResult` (or accept the
  clients/session-factory and build the probes internally) — the probes are **injected** so the logic
  is unit-testable with fakes and needs **no** live stack.
- Each probe is **read-only and bounded**:
  - **Postgres** — `SELECT 1` on a short-lived session.
  - **Neo4j** — `Neo4jClient.verify()` (`verify_connectivity`).
  - **MinIO** — a read-only `head_bucket` on the landing bucket **via the existing client**
    (`landing.client.head_bucket(Bucket=landing.bucket)`); **never** a write, **never** `ensure_bucket`
    (so `storage/landing.py` is NOT touched).
- Each probe is wrapped so a failure records that component `"down"` (not a 500). `/ready` returns
  **200** + `{"ready": true, "checks": {"postgres":"ok","neo4j":"ok","minio":"ok"}}` **iff all** pass,
  else **503** with the failing component(s) named. Unauthenticated (a probe endpoint, like `/health`;
  add `/ready` to `DEFAULT_PUBLIC_PATHS` in `api/middleware.py` — **wait**: that edit is OUT of scope,
  see note). 

  > **Public-path note.** `/ready` must be reachable unauthenticated. `api/middleware.py`
  > `DEFAULT_PUBLIC_PATHS` currently lists `/health,/docs,/redoc,/openapi.json`. Adding `/ready`
  > requires touching `middleware.py`. This file **IS** added to the allow-list (§ scope) for this one
  > line. The builder makes the **minimal** change (add `"/ready"` to the frozenset) and MUST NOT alter
  > any auth logic; `tests/unit/test_api_health.py`'s auth assertions stay green (FROZEN).

### 3.3 Driver last-tick heartbeat + `--healthcheck`

`src/worldmonitor/runner/heartbeat.py` (NEW, pure, fully covered):

- `Heartbeat(path: Path, stale_after_seconds: float)`:
  - `touch(now: datetime) -> None` — atomic write (temp file + `os.replace`) of the ISO timestamp;
    creates the parent dir if missing.
  - `read(now=...) -> datetime | None`; `is_alive(now: datetime) -> bool` = file exists **and**
    `now - last_tick <= stale_after_seconds`. Missing/unparseable file ⇒ **not alive**.
- `src/worldmonitor/runner/driver.py`:
  - `IngestDriver.run_forever` calls `self._heartbeat.touch(now)` **once per loop iteration** (every
    tick, regardless of ingest/resolve work — an *idle* driver is alive). The single `touch` call stays
    inside the existing `# pragma: no cover` loop; **all** staleness logic lives in `heartbeat.py`.
  - `build_driver` wires a `Heartbeat` from settings (`driver_heartbeat_path`,
    `driver_heartbeat_stale_seconds`).
  - `main()` grows a `--healthcheck` mode: build the `Heartbeat` from settings, `is_alive(now)` →
    **exit 0** (alive) / **exit 1** (missing or stale). This is the container `HEALTHCHECK` command.
    `--healthcheck` must NOT construct the full driver (no store connections) — it only reads the file.

### 3.4 `settings.py` knobs (12-factor, safe defaults)

- `driver_heartbeat_path: str = "/var/run/worldmonitor/driver.heartbeat"`.
- `driver_heartbeat_stale_seconds: float = Field(default=..., gt=0)` — a safe multiple of
  `driver_tick_seconds` (the driver ticks every `driver_tick_seconds`, default 30; default staleness
  e.g. `90.0` ≈ 3 missed ticks). Document the relationship.
- `readiness_probe_timeout_seconds: float = Field(default=5.0, gt=0)` — bound each `/ready` probe.

### 3.5 Containerization (D1)

- `Dockerfile` (NEW): slim Python 3.12 base, install via `uv` (the project uses `uv`; `pyproject.toml`
  + the package under `src/worldmonitor`), non-root user, no secrets baked in (12-factor — all config
  via env). Default `CMD` may be the API; the compose services set explicit `command`s.
- `.dockerignore` (NEW): exclude `.venv`, `.git`, tests, caches, `*.md` build noise, `.env`.
- `deploy/compose.yaml`: add **two** services from the one image:
  - `driver`: `command: python -m worldmonitor.runner.driver`; `restart: unless-stopped`;
    `depends_on: {postgres, neo4j, minio: condition: service_healthy}`;
    `mem_limit: ${WM_DRIVER_MEM_LIMIT:-1g}`; env mapping the stores to the **service hostnames**
    (`POSTGRES_DSN`/`NEO4J_URI`/`MINIO_ENDPOINT` → `postgres`/`neo4j`/`minio`), `NEO4J_PASSWORD`,
    `CONFIG_ENCRYPTION_KEY`, the heartbeat path; a `HEALTHCHECK` running
    `python -m worldmonitor.runner.driver --healthcheck`.
  - `api`: `command: uvicorn worldmonitor.api.main:create_app --factory --host 0.0.0.0 --port 8000`;
    `restart: unless-stopped`; same `depends_on`; `mem_limit: ${WM_API_MEM_LIMIT:-1g}`; ports
    `8000:8000`; a `HEALTHCHECK` that curls `/ready` (e.g. `curl -fsS http://localhost:8000/ready`).
- **Supervision honesty (state it in the runbook).** `restart: unless-stopped` restarts on process
  **exit** (covers OOM-kill / crash / reboot — the core B-4 fix); plain Compose does **not**
  auto-restart on *unhealthy*. The healthcheck gives `depends_on` ordering + makes a *stall* observable
  (`compose ps` → `unhealthy`) + is the hook for a future k8s liveness probe / autoheal sidecar.
  Restart-on-stall is a named follow-up (§11).

---

## 4. Acceptance criteria (crisp APPROVE bar)

1. `tests/unit/test_api_ready.py` proves: `/ready`→**200** + all-`ok` body when all probes pass;
   `/ready`→**503** naming the down component when **any** one probe fails (one test per store).
2. `tests/unit/test_driver_heartbeat.py` proves: a fresh heartbeat ⇒ `is_alive` True / `--healthcheck`
   exit 0; a stale timestamp ⇒ `is_alive` False / `--healthcheck` exit **1**; a missing file ⇒ not
   alive / exit **1**; `touch` writes atomically and is re-readable.
3. `/health` is unchanged and `tests/unit/test_api_health.py` stays green.
4. `Dockerfile` builds; `deploy/compose.yaml` `config` interpolates; `compose-boot` brings `api` +
   `driver` to **healthy** (`up --wait`) — the `api` healthcheck (`/ready`) and the `driver`
   healthcheck (`--healthcheck`) both pass against the real stack; `driver`/`api` start only after the
   stores are healthy.
5. The `driver` service has `restart: unless-stopped` **and** a `mem_limit`.
6. No migration; `db/models.py` unchanged; the FROZEN suites (§9) stay green.

---

## 5. Failing-test-first (RED → GREEN)

- `tests/unit/test_api_ready.py` calls `client.get("/ready")` → **404 today** (no route) → **RED**.
  GREEN once `/ready` + `readiness.py` exist.
- `tests/unit/test_driver_heartbeat.py` does `from worldmonitor.runner.heartbeat import Heartbeat`
  (and/or invokes `driver --healthcheck`) → **ImportError today** (no module / no flag) → **RED**.
  GREEN once `heartbeat.py` + the `--healthcheck` mode exist.

Stated explicitly so the RED→GREEN signal is unambiguous, exactly as B-4a/B-4b were failing-first.

---

## 6. compose-boot wiring (no new CI job)

Extend `.github/workflows/compose-boot.yml`:

- Add a build of the app image before `up` (or rely on compose `build:` on the `api`/`driver`
  services; `up --wait` will build).
- Add `api driver` to the `up -d --wait` service list (alongside `postgres neo4j minio`).
- **CONFIG_ENCRYPTION_KEY**: the driver's `build_driver` constructs a `ConfigCipher` eagerly, which
  needs a **valid Fernet key**. compose-boot's `.env` writer currently fills required vars with
  `ci-unused`. Add a generated Fernet key for `CONFIG_ENCRYPTION_KEY` to the `known` map (mirroring how
  it sets `NEO4J_PASSWORD`) so the driver boots. (On a fresh CI stack there are no `ConnectorInstance`
  rows, so the driver ticks idle and stays healthy.)
- Keep the existing teardown (`down -v`). No new workflow file.

---

## 7. Migration conclusion

**NONE.** The heartbeat is a **file** (per-container), not a table; `/ready` only **reads** the stores.
No new table/column/constraint. `tests/integration/test_migrations.py` (alembic head == create_all,
ADR 0030) is **not triggered and MUST stay green** (FROZEN). `db/models.py` is **not** touched (a
Postgres heartbeat table was considered and rejected — ADR 0051 alternative A).

---

## 8. Person-affecting / sign-off assessment

**Ops / deployment — NOT person-affecting.** Supervision + health surfaces merge nothing, score
nothing, mutate no threshold, and touch no real-person record. No per-run human sign-off. `/ready` and
the heartbeat are read-only on the stores (the only new write is the heartbeat **file**, outside all
three stores). `human_fork: false` (determinable from CLAUDE.md + the existing code; no OPEN question).

---

## 9. FROZEN (keep-green, unchanged)

A removed assert / added skip|xfail / loosened tolerance here is a judge **DENY** (D-FROZEN). This gate
adds health/supervision; it changes no write/merge/score/recovery path.

- `tests/unit/test_api_health.py` — `/health` stays a public liveness echo (do NOT change it to assert
  store state; do NOT weaken its auth assertions).
- `tests/integration/test_migrations.py` — alembic head == create_all drift guard (ADR 0030); no
  migration is added.
- the driver behaviour suites — `run_due_ingests` / `run_resolution` (serialization) / `recover_stale`
  / `prune_task_runs` semantics (ADR 0029) must be byte-for-byte unchanged; the heartbeat `touch` is
  additive and must not alter cadence, serialization, or startup recovery.
- the resolution + sign-off suites (`cluster_and_merge`, `ResolverJudgement`, `SignOff`, H-1/H-2).
- Gate C value-level provenance (witness-map fusion / `prov_*` projection / edge provenance).
- the graph writer suite (`write_entities` nodes+edges+topic-labels, G1 projection).
- the sensitivity guard suite (Gate E / ADR 0047).

---

## 10. Locked invariants the gate must hold + APPROVE/DENY

- **G1 — provenance on every node AND edge.** PRESERVED **VACUOUSLY** — supervision/health adds no
  graph write; `/ready` and the heartbeat read only (Postgres `SELECT 1`, Neo4j `verify`, MinIO
  `head_bucket`, a heartbeat file). **DENY** if any writer / provenance / projection path is touched or
  any G1 test regresses.
- **Append-only / no un-merge.** PRESERVED **VACUOUSLY** — nothing here mutates Neo4j, the
  `canonical_id_ledger`, or any Postgres table; the only new write is a heartbeat **file**. **DENY** if
  any store mutation (or a migration) is introduced.
- **Canonical-canonical only via the guard.** PRESERVED **VACUOUSLY** — no resolver, no merge, no
  threshold, no `cluster_and_merge`, no sensitivity-guard path is touched. **DENY** if any
  merge/threshold/score/guard code changes.
- **The gate's OWN positive invariant (load-bearing).** The new surface distinguishes a live pipeline
  from a dead one: `/ready` is 503 when a store is down (and 200 when all up), and a stale heartbeat
  makes the liveness check report **down** while `/health` still echoes ok. **DENY** if `/ready` cannot
  go not-ready, or the heartbeat cannot go stale-detected, or `/health` is changed to mask the
  distinction.

**APPROVE** iff: `/ready` flips 200↔503 on store reachability with a per-component body; the heartbeat
goes stale → `--healthcheck` exits non-zero while `/health` still says ok; `/health` + the FROZEN
suites stay green; the `Dockerfile` builds and `compose-boot` brings `api`+`driver` healthy after the
stores; the `driver` service has `restart: unless-stopped` + a `mem_limit`; no migration. **DENY** on
any FROZEN regression, any store-mutation/migration, or a change that re-hides a dead pipeline behind a
green signal.

---

## 11. Out of scope (hard stops)

- **NO** dead-letter (`ingest_dead_letter`) pruning/retention (B-4d / M-6).
- **NO** new backup/restore work (B-4b / ADR 0050 done).
- **NO** Postgres/Redis heartbeat store; **NO** migration; **NO** `db/models.py` edit.
- **NO** change to `/health` semantics; **NO** removal/weakening of `test_api_health.py`.
- **NO** multi-replica lease / HA (ADR 0029 fork X2); **NO** autoheal sidecar / Swarm / k8s
  liveness auto-restart-on-stall (named follow-up — `restart: unless-stopped` covers crash/OOM/reboot).
- **NO** GeoNames streaming fix (H-6) — `mem_limit` only bounds the blast radius here.
- **NO** change to ingest/resolve cadence, serialization, startup recovery, or any `pipeline.py` /
  `merge.py` / `writer.py` / resolver / guard code.
- **NO** edit to `storage/landing.py` (the `/ready` MinIO probe uses the existing client's read-only
  `head_bucket`); **NO** edit to `db/models.py`.
- **NO** new CI job (extend `compose-boot` only).
- `api/middleware.py` is touched for the **single** minimal line that makes `/ready` public — no other
  change to auth logic.

---

## 12. Slice plan

Three independent, individually-mergeable slices, each failing-test-first. Slices 1 and 2 need **no
Docker** (pure unit). Slice 3 is verified by `compose-boot`. Slices 1 and 2 both add fields to
`settings.py` (distinct fields — coordinate, no conflict).

- **Slice 1 — `/ready` store-probe.** `src/worldmonitor/api/readiness.py` (NEW) + `api/main.py`
  (`/ready` route) + `api/middleware.py` (add `/ready` to public paths, one line) + `settings.py`
  (`readiness_probe_timeout_seconds`) + `tests/unit/test_api_ready.py`. RED: `/ready`→404. Mergeable
  alone.
- **Slice 2 — driver heartbeat.** `src/worldmonitor/runner/heartbeat.py` (NEW) + `runner/driver.py`
  (`touch` per tick + `--healthcheck` + `build_driver` wiring) + `settings.py`
  (`driver_heartbeat_path`, `driver_heartbeat_stale_seconds`) + `tests/unit/test_driver_heartbeat.py`.
  RED: import/flag missing. Mergeable alone.
- **Slice 3 — containerize + supervise + verify.** `Dockerfile` + `.dockerignore` (NEW) +
  `deploy/compose.yaml` (`api` + `driver` services) + `.github/workflows/compose-boot.yml` (build +
  `up --wait api driver` + Fernet key) + `.env.example` (new vars + mem limits) +
  `docs/runbooks/driver-supervision.md` (NEW). Optional: `tests/integration/test_ready_probes.py`
  (testcontainers). References the `/ready` route and `--healthcheck` from slices 1+2, so merge it
  **after** them (or stub the healthcheck commands and let CI go green once 1+2 land). Verified by
  `compose-boot`.

`human_fork: false`. No OPEN architectural question — ops hardening; the one genuine decision (heartbeat
storage + which surface exposes driver liveness) is decided in ADR 0051 (file-based per-container
heartbeat + `--healthcheck`; `/ready` = store reachability) with alternatives recorded; not a product
fork, so PROPOSED, no human STOP.

---

## 13. Verdict

**Build slices 1 → 2 → 3.** The gate closes B-4's supervision + dead-pipeline-detection defects with
the smallest correct footprint: a real `/ready` (store reachability, fail-closed), a file-based driver
heartbeat with a `--healthcheck` container probe (the live-vs-dead distinction the audit demands),
and a single app image supervised in compose (`restart` + `mem_limit` + `depends_on: service_healthy`),
all verified by the existing `compose-boot` CI job. No migration, no store mutation, no change to the
merge/score/guard paths — the invariants hold vacuously and the new positive invariant (a dead pipeline
is detectable) is the APPROVE bar. Not a product fork; ADR 0051 is PROPOSED, no human STOP.
</content>
