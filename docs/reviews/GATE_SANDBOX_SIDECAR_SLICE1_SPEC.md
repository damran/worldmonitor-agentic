# Gate — Sandbox-runner sidecar, SLICE 1 (app seam + service code)

- Gate: Sandbox-runner sidecar **Slice 1** (the locally-verifiable half of the Stage-4 container/egress sandbox)
- Branch: `feat/sandbox-runner-sidecar` (off `master`)
- ADR: `docs/decisions/0077-sandbox-runner-sidecar.md` (accepted; the isolation-primitive fork = sidecar, decided with the user)
- Person-affecting: **NO** (operator-gated active-recon execution plumbing). No human sign-off.
- Migration: **NONE**. Flag default-off ⇒ no prod behaviour change from this slice.
- Scope contract: `.claude/gate.scope` (INV-1..8). **Slice 2 = the Dockerfile/compose/nftables deploy infra, NOT here.**

---

## 1. GAP
`container_sandbox_enabled` is a pure refusal toggle (`operator_run.run_connector_once:104-110` raises
`SandboxUnavailableError`→409 for `sandbox=="container"`). There is no execution backend: all tools run
via `run_command` (`runner/subprocess.py`) as host subprocesses. Flipping the flag today would run nmap
**un-sandboxed on the host**. This slice makes container-level tools route through a sandbox-runner
**sidecar** over HTTP (the sidecar's container/egress/image is Slice 2), behind the default-off flag.

## 2. BUILD (per ADR 0077 §D1–D3, §D6 Slice 1)

### 2.1 Settings (`settings.py`)
- `sandbox_runner_url: str = ""` — the sidecar base URL (empty ⇒ container routing unavailable).
- `sandbox_runner_secret: SecretStr = SecretStr("")` — shared secret for the `POST /run` auth header
  (from env). Do **NOT** add it to `validate_production_secrets` (ADR 0061 stays frozen) — it is
  required at the routing point (§2.3), not at boot. `.env.example`: `SANDBOX_RUNNER_URL=` (empty) +
  `SANDBOX_RUNNER_SECRET=` under a `# --- Sandbox-runner sidecar (ADR 0077) ---` block.

### 2.2 App-side `ContainerRunner` (`src/worldmonitor/sandbox/container_runner.py`)
A `Runner`-compatible async callable (the `cli_tool.Runner` type — `runner(argv, *, timeout) ->
RunResult`). `make_container_runner(settings) -> Runner` (or a small class) that:
- POSTs JSON `{"argv": [...], "timeout": <float>}` to `f"{sandbox_runner_url}/run"` with header
  `X-Sandbox-Secret: <sandbox_runner_secret>` (use the existing httpx client style in the repo; bound
  the request with its own client timeout ≥ the tool timeout).
- Parses the JSON response into a **`RunResult`** (same fields `run_command` returns:
  returncode/stdout/stderr/timed_out/duration — match the dataclass exactly; decode stdout/stderr
  consistently with `run_command`, i.e. bytes/str as the contract requires).
- On a transport error / non-2xx / malformed body → raise a clear `RuntimeError` (a runner failure that
  surfaces as a recorded run error) — **never** a silent empty-success `RunResult`.
- argv is passed through as a **list** (no shell, no string join).

### 2.3 Routing (`runner/operator_run.py`, and a clean seam on `cli_tool.py` if needed)
Replace the refuse-only gate with refuse-or-route:
```
if connector.sandbox == "container":
    if not settings.container_sandbox_enabled:
        raise SandboxUnavailableError(... "not enabled")          # INV-1, unchanged
    if not settings.sandbox_runner_url or not settings.sandbox_runner_secret.get_secret_value():
        raise SandboxUnavailableError(... "enabled but no sandbox-runner configured")  # INV-2
    <inject the ContainerRunner as the connector's runner>         # INV-3
```
Inject via a clean seam — e.g. the connector already takes `runner=` in `__init__` (cli_tool.py:65); add
a minimal public way to set it post-construction (a `use_runner(runner)` method or equivalent) OR have
`run_connector_once` construct/replace the runner. Do NOT break the existing test seam where a fake
runner is injected via `runner=`. subprocess-level tools are untouched (still `run_command`).

### 2.4 Sidecar service code (`src/worldmonitor/sandbox/runner_service.py`)
A FastAPI app (factory `create_sandbox_app()` for testability) exposing:
- `GET /health` → `{"status": "ok"}`.
- `POST /run` — header `X-Sandbox-Secret` compared **constant-time** (`hmac.compare_digest`) to the
  service's configured secret (401 on mismatch/missing). Body `{argv: list[str], timeout: float}`:
  reject (422/400) if `argv[0]` ∉ `{"nmap","dig","whois"}`, argv is not a `list[str]`, or timeout is
  out of bounds. On success run `run_command(argv, timeout=timeout)` and return the `RunResult` as JSON.
  It re-validates **independently** of the caller (defense-in-depth, ADR 0077 §D2). The service reads
  its secret + bounds from settings/env. (The container/egress/image is Slice 2 — here it is just an
  ASGI app + validator unit-tested with a fake `run_command`.)

## 3. INVARIANTS — see `.claude/gate.scope` INV-1..8.

## 4. FAILING-TEST-FIRST (RED → GREEN)
- `tests/unit/test_settings.py` — `sandbox_runner_url` default ""/override; `sandbox_runner_secret`
  default empty/override (and that it's a SecretStr, not echoed in repr).
- `tests/unit/test_container_runner.py` — with a FAKE httpx transport/client: asserts it POSTs the argv
  list + timeout + the secret header to `<url>/run` and maps the JSON to a correct `RunResult`; a non-2xx
  / transport error / malformed body raises (no silent success); argv stays a list.
- `tests/unit/test_sandbox_runner_service.py` — `create_sandbox_app()` via `TestClient` + a fake
  `run_command`: `/run` with an allowlisted tool + correct secret → RunResult JSON; wrong/missing secret
  → 401 (constant-time); tool ∉ allowlist → 4xx; non-list/shell argv → 4xx; `/health` → ok. The fake
  `run_command` proves no shell + that validation happens before exec.
- `tests/integration/test_operator_run_sandbox_routing.py` (or extend `test_active_run_sandbox_ui.py`):
  INV-1 flag off → 409/refused (the existing assertion stays); INV-2 flag on + url empty → refused;
  INV-3 flag on + url+secret set → the container connector executes via the ContainerRunner (assert via
  a fake sidecar/fake runner that the sidecar path ran, NOT `run_command` on the host) and lands a row;
  a subprocess tool (dig/whois) is unaffected.

## 5. ACCEPTANCE
- The two settings exist + documented; the ContainerRunner + sidecar service modules exist with the
  contracts above; routing per INV-1/2/3; the sidecar independently validates (INV-5); argv stays a list
  everywhere (INV-6). All §4 tests green; existing `test_active_run_sandbox_ui.py` + the ADR-0072
  security tests stay green; `db/models.py` + `validate_production_secrets` untouched; ruff/pyright clean.

## 6. FROZEN
- ADR 0072 nmap-refused-when-flag-off + the target validator + enforced allowlist; ADR 0071 cadence-driver
  ACTIVE refusal; ADR 0061 `validate_production_secrets`; the `run_command` host-subprocess contract for
  subprocess-level tools; `db/models.py` (no schema change).

## 7. OUT OF SCOPE (Slice 2 / later)
- The `Dockerfile` tool binaries, the `sandbox-runner` compose service, `CAP_NET_ADMIN` + the nftables
  egress (deny RFC1918 + metadata / allow public), resource limits, the internal-network wiring,
  compose-boot smoke — **all Slice 2** (CI/deploy-verified; this WSL box can't build images).
- Per-run target-only egress; moving whois/dig into the sandbox; runtime-health checks; gVisor/Kata.

## 8. PERSON-AFFECTING / VERDICT
**NOT person-affecting → no human sign-off** (`human_fork: false`; the isolation-primitive fork was
already decided with the user in ADR 0077). Operator-run-only execution plumbing; no ER/merge/score/
graph; no `@given` invariant mandated. Reversible (flag default-off). One PR for Slice 1; checker
reproduces INV-1..8; judge gates.
