# Gate — Sandbox-runner sidecar, SLICE 2 (deploy infra + argv hardening)

- Gate: Sandbox-runner sidecar **Slice 2** (the deploy half; completes the Stage-4 container/egress sandbox)
- Branch: `feat/sandbox-sidecar-slice2` (off `master`)
- ADR: `docs/decisions/0077-sandbox-runner-sidecar.md` (the "Slice 2 — deploy & egress" section is authoritative)
- Person-affecting: **NO** (operator-run active-recon deploy plumbing). No sign-off.
- Migration: **NONE**. `container_sandbox_enabled` stays default-off ⇒ no prod behaviour change.
- Scope contract: `.claude/gate.scope` (INV-1..8). Real container exec + network isolation are CI compose-boot-verified (this box can't build images).

---

## 1. GAP
Slice 1 landed the app seam + sidecar service code (dormant, default-off). Nothing is deployed: the app
image lacks nmap/dig/whois, there is no `sandbox-runner` service, the compose stack is a single bridge
(no egress isolation), and the sidecar's validator allows arbitrary `argv[1:]` flags (judge HIGH).

## 2. BUILD

### 2.1 argv flag-hardening (LOCALLY TESTABLE) — `src/worldmonitor/sandbox/runner_service.py`
Extend `POST /run` validation (after the existing `argv[0] ∈ {nmap,dig,whois}` + list[str] + timeout
checks) with a per-tool DEFAULT-DENY allow-set on the MIDDLE tokens + a target check on the LAST token:
- `_ALLOWED_MIDDLE = {"nmap": frozenset({"-oX","-","--"}), "dig": frozenset({"+short","--"}), "whois": frozenset({"--"})}`.
- Require `len(argv) >= 2`; every token in `argv[1:-1]` ∈ `_ALLOWED_MIDDLE[argv[0]]`; the last token
  (the target) matches the host/IP validator (reuse the `cli_tool` target rule: regex `[A-Za-z0-9.:-]+`,
  length ≤ 253, must NOT start with `-`, must NOT contain `/`). Any violation → 422, `run_command` NOT
  called. (Intentional coupling: a new connector flag means updating the allow-set — a safe explicit edit.)

### 2.2 Dockerfile — a dedicated `sandbox-runner` stage
`FROM runtime AS sandbox-runner` then apt-install `nmap dnsutils whois` (this stage MAY use apt — it is a
separate build target; the default runtime/api/driver image stays apt-free + slim). Stay non-root (the
existing `worldmonitor` user). `CMD` = `uvicorn worldmonitor.sandbox.runner_service:create_sandbox_app
--factory --host 0.0.0.0 --port <PORT>` (e.g. 9101). Mirror the apt-hardening flags the builder stage uses.

### 2.3 compose — the `sandbox-runner` service + network isolation
- Top-level: add `networks: { sandbox_net: {} }` (the implicit `default` remains for the stores).
- `sandbox-runner` service: `build: {context: .., dockerfile: Dockerfile, target: sandbox-runner}`,
  `networks: [sandbox_net]` **only** (NOT default ⇒ cannot reach the stores), `restart: unless-stopped`,
  non-root, `read_only: true` + `tmpfs: [/tmp]`, `mem_limit`/`pids_limit`/`cpus`/`ulimits: {nofile}`,
  `environment: { ENVIRONMENT, SANDBOX_RUNNER_SECRET }`, a `/health` healthcheck (python urllib or a
  curl-free check), **no `ports:`** (in-network only). `cap_drop: [ALL]` (no caps needed — network
  isolation does the egress control).
- `api` + `driver`: add `networks: [default, sandbox_net]` and `environment` entries
  `SANDBOX_RUNNER_URL: http://sandbox-runner:9101` + `SANDBOX_RUNNER_SECRET: ${SANDBOX_RUNNER_SECRET}`.
  (Adding `networks:` to api/driver makes their network membership explicit — they MUST list `default`
  too or they lose store reachability.) Leave `CONTAINER_SANDBOX_ENABLED` default-off (operator opts in).
- `.env.example`: keep `SANDBOX_RUNNER_URL`/`SANDBOX_RUNNER_SECRET` (Slice 1) + note the compose default.

## 3. INVARIANTS — see `.claude/gate.scope` INV-1..8.

## 4. FAILING-TEST-FIRST (RED → GREEN)
- `tests/unit/test_sandbox_runner_service.py` (extend) — the argv allowlist: each connector's real argv
  passes (`["nmap","-oX","-","--","example.com"]`, `["dig","+short","--","example.com"]`,
  `["whois","--","example.com"]`); and these are REJECTED (4xx, `run_command` not called):
  `["nmap","--script","vuln","--","x"]`, `["nmap","-oN","out.txt","--","x"]`, `["nmap","-iR","100"]`,
  `["nmap","-oX","-","--","../etc/passwd"]` (target has `/`), `["nmap","-oX","-","--","-oG"]` (target
  leading `-`), `["dig","+time=1","--","x"]` (unknown middle flag), an empty/just-tool argv.
- `tests/unit/test_compose_sandbox_topology.py` (NEW, locally runnable — parse `deploy/compose.yaml`
  with PyYAML, no image build): assert the `sandbox-runner` service exists with `build.target ==
  "sandbox-runner"`; its `networks` includes `sandbox_net` and does NOT include `default`; the store
  services (postgres/neo4j/minio/redis/zitadel) are NOT on `sandbox_net`; `sandbox-runner` has no
  `ports:`; it carries mem/pids limits + a `read_only` flag + a healthcheck; `api` and `driver` list
  both `default` and `sandbox_net` and define `SANDBOX_RUNNER_URL` + `SANDBOX_RUNNER_SECRET`. (This guards
  the egress-isolation INV-2/INV-3/INV-4/INV-5 invariants without building images.)

## 5. ACCEPTANCE
- The argv allowlist rejects all the §4 dangerous forms and passes the connectors' argv; the compose
  topology test is green; existing Slice-1 tests stay green; ruff/pyright clean. CI `compose-boot` brings
  the stack (incl. `sandbox-runner`) up healthy (the real image-build + network proof).

## 6. FROZEN
- All of Slice 1 (ContainerRunner, operator_run routing, settings, `cli_tool.use_runner`, the
  fail-closed empty-secret guard) — byte-identical; only `runner_service.py`'s validator grows.
- ADR 0061 `validate_production_secrets`; `db/models.py` (no schema change); the api/driver IMAGE staying
  apt-free (tools only in the sandbox-runner stage).

## 7. OUT OF SCOPE / DEFERRED
- nftables / `CAP_NET_ADMIN` metadata+broad-RFC1918 denial (egress is network-isolation in v1; ADR §D4
  refinement — revisit for cloud/metadata). Per-run target-only egress. moving whois/dig into the sandbox.
  A real Prometheus server (H-8c ops). Auto-enabling `container_sandbox_enabled`.

## 8. PERSON-AFFECTING / VERDICT
**NOT person-affecting → no sign-off** (`human_fork: false`). Deploy plumbing + a default-deny validator;
reversible (default-off flag + deploy config). One PR; checker reproduces INV-1..8; judge gates; CI
compose-boot is the live integration proof.
