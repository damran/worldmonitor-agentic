# 0071 — ACTIVE-capability gating: scope token + operator-run path + CliToolConnector/whois (Phase-2 Stage-3 slice 6a)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/3g-active-gating-whois` (off `master`). The active-operation security boundary. Decided
  with the user (sandbox = subprocess/args-safe for v1; trigger = REST [UI button in 6b]; tools whois→6a,
  dig+nmap→6b).
- **human_fork:** false (the model below was decided with the user; details are reversible).

## Context

`CLAUDE.md`: *"Active plugins are gated: authorized-scope token per run, separate logging, never
agent-auto-run."* The driver **refuses every ACTIVE connector** on the cadence path
(`driver.py::ActiveConnectorRefused`). This slice builds the **authorized path**: an operator can run an
ACTIVE connector once, for a specific scope, with a tamper-evident audit record — while the cadence (and any
agent) still cannot. The first ACTIVE connector is **whois** (read-only registration lookup) via the
existing `run_command` (argv-list, no shell, hard timeout). Container/egress-sandbox is a **documented
follow-up** (the user chose subprocess for v1; heavy tools like nmap will declare a container requirement and
stay execution-gated until it lands — slice 6b).

## Decision

### 1. Scope token (`plugins/scope_token.py`) — a tamper-evident per-run authorization
A Fernet-signed (reusing `ConfigCipher`'s key for v1 — a dedicated key is a noted refinement) token over the
claims `{connector_id, instance_id, scope, operator, issued_at, expires_at, nonce}`:
- `mint(connector_id, instance_id, scope: dict, operator: str, *, ttl_seconds=3600) -> str`.
- `verify(token, *, expected_connector_id, expected_instance_id) -> dict` — decrypt; reject if the
  connector/instance don't match, if expired, or if malformed. Returns the claims.
The token is **minted and consumed server-side in the same operator-triggered run** (it is not a wire
credential handed to a client) and **stored on the `task_run`** as the audit proof of *what was authorized
by whom*.

### 2. Audit columns on `TaskRun` (migration `0008_task_run_audit`)
- `run_mode: str` (default `"cadence"`; `"operator"` for an operator-triggered run),
- `triggered_by: str | None` (the authenticated operator subject),
- `scope_token: str | None` (the minted token — the tamper-evident authorization record).
Every ACTIVE run is therefore queryable by operator + scope. **Separate logging:** an ACTIVE run logs at a
distinct, higher-visibility marker (a dedicated `worldmonitor.active` logger line carrying connector/instance/
operator — never the token plaintext beyond the audit column).

### 3. The operator-run path (`runner/operator_run.py::run_connector_once`)
A function callable from the REST endpoint (and later the UI), DISTINCT from the cadence `_ingest_instance`:
- Loads the instance + connector; decrypts config.
- **ACTIVE** → a `scope` is **required** (else refuse, 422); `mint` a scope token, `verify` it (defense in
  depth), inject `config["_scope"] = scope`, and run with `run_mode="operator"`, `triggered_by=operator`,
  `scope_token=<token>`.
- **PASSIVE** → an operator may run-now without a token (`run_mode="operator"`, no token).
- Runs via the existing `run_ingest` (landing + ER queue, unchanged). Records the `TaskRun` audit fields.
- The **cadence** path (`run_due_ingests` → `_ingest_instance`) is **UNCHANGED** — it still refuses ACTIVE.
  This path is reachable ONLY from the authed REST endpoint (an operator) — **never the cadence, never an
  agent/MCP tool** (so "never agent-auto-run" holds).

### 4. REST trigger (`api/integrations.py::POST /integrations/instances/{id}/run`)
`get_principal`-gated + **CSRF**-protected (a state-changing browser POST). Body: a `scope` (the target +
limits; required for ACTIVE, optional for PASSIVE). Calls `run_connector_once(..., operator=principal.subject)`;
303 back to `/integrations`. ACTIVE without a scope → 422. (The UI "Run (active)" button is slice 6b.)

### 5. `CliToolConnector` base (`plugins/cli_tool.py`) + whois
`CliToolConnector(Connector)`, **`Capability.ACTIVE`**. `collect(config)` reads `config["_scope"]`,
**validates the scope's target** (subclass hook `_validate_target` — strict shape), builds an **argv list**
(subclass hook `_build_argv(scope) -> list[str]` — NEVER a shell string), runs it via
`run_command(argv, timeout=…)` (async, driven from sync `collect()` via `asyncio.run`), and yields a
`RawRecord` of the captured stdout. The whois connector: scope `{target: <domain|ip>}`; the target is
validated against a strict `^[A-Za-z0-9.:-]+$` (a domain or IP — no whitespace, no shell metachars, no
leading `-`), and the argv is `["whois", "--", target]` (the `--` blocks flag-injection even if validation
were bypassed). `map()` does a minimal, fail-soft parse of the whois text → an FtM `Organization` (the
registrant) + the domain + provenance.

### 6. Safety (active command execution — the threats)
- **No shell, no arg-injection:** argv is always a list (`run_command` uses `exec`, never a shell); the
  target is strictly validated AND a `--` terminator precedes it; a hostile target (`"-oX /x"`,
  `"; rm -rf"`, `"$(...)"`, `"a b"`) is rejected by validation and, even if not, cannot become a flag or a
  shell command. **Bounded** by `run_command`'s timeout.
- **Authorization:** an ACTIVE run requires a valid `scope` and mints/stores a scope token; the cadence and
  agents cannot reach this path. Operator identity is authenticated (`get_principal`) and audited.
- **Hostile output:** the tool's stdout is `RawRecord.data` (hostile bytes); `map()` validates via FtM and
  fail-soft-skips garbage. Read-only into landing + ER queue; never the graph.
- **No container yet (honest limitation):** v1 runs in a subprocess (no egress constraint) — acceptable for
  a low-risk read-only tool (whois); heavy tools (nmap) declare a container requirement and are
  execution-gated until the sandbox lands (6b + a Stage-4 container fork).

## Alternatives considered
- **JWT / HMAC scope token vs Fernet.** Fernet reuses `ConfigCipher` (authenticated encryption, key rotation
  via MultiFernet); a JWT is fine too. Fernet chosen for reuse; a dedicated key is a noted refinement.
- **Enqueue the run for the driver vs run synchronously in the API.** Synchronous is simplest for a quick
  read-only tool + lets the operator see the result; a queued/async run is a later option for long tools.
- **Allow the cadence to run ACTIVE with a stored token.** No — "never agent-auto-run"; ACTIVE is
  operator-triggered only. The cadence stays refusing.

## Consequences
- The platform can run an authorized ACTIVE tool, once, for a scope, with a tamper-evident audit — the gate
  CLAUDE.md mandates. The cadence + agents still cannot. Reusable: the scope-token + operator-run + audit work
  for every future ACTIVE connector.
- **Migration `0008_task_run_audit`** (drift guard passes). New module + a REST route + the connector base +
  whois. **Not person-affecting** (operator op; no ER/merge/score). **Single-tenant.**

## Reversibility
Reversible — drop the route + the operator-run + the connector; the `task_run` audit columns are inert for
cadence runs (`run_mode` defaults `"cadence"`). Reversal cost: low-medium (one migration). Revisit triggers:
heavy/hostile tools → the container+egress sandbox; agent-requested active runs → an approval queue
(human sign-off, never auto); a dedicated scope-token key.

## Invariant gate note
Active-execution security boundary — not an ER/merge invariant, so no `@given`. **Security failing-test-first:**
(a) the CADENCE path STILL refuses ACTIVE (`ActiveConnectorRefused`, frozen); (b) the operator-run REFUSES an
ACTIVE run with no scope (422) and runs it only with a minted+verified scope token; (c) scope-token integrity —
a tampered / expired / wrong-connector / wrong-instance token is rejected; (d) ARG-INJECTION SAFETY — a
hostile scope target (`-oX …`, `; rm`, `$(…)`, whitespace, leading `-`) is rejected by `_validate_target`, the
argv stays a list, and no shell/flag injection is possible (reproduced: the tool receives the literal target
as one arg after `--`, or the run is refused); (e) the `task_run` records `run_mode="operator"` +
`triggered_by` + `scope_token`; the run logs on the separate ACTIVE marker; (f) the REST route is
`get_principal`-gated + CSRF-protected; (g) the migration drift guard passes; (h) whois `map()` is fail-soft.
All over a FAKE run_command (no real `whois` binary / no network) + a testcontainer Postgres.
