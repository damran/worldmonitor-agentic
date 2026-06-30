# 0093 — Phase-3 S3b: deploy the Hermes agent layer + the MCP HTTP server as compose services

- **Status:** Accepted (2026-06-30)
- **Date:** 2026-06-30
- **Gate:** Phase-3 slice **S3b** (give the remote Hermes container a compose home and wire it to our
  MCP read surface (S1) + LLM endpoint (S3a)). Companion spec:
  `docs/reviews/GATE_S3B_HERMES_DEPLOY_SPEC.md`. Umbrella: ADR 0089 (D2/D3).
- **Milestone:** Phase 3 (`docs/40_ROADMAP.md`). Depends on S1 (ADR 0090, MCP HTTP+bearer, merged
  `497296c`), S2 (ADR 0091, the in-process LLM gateway) and S3a (ADR 0092, the `/v1/chat/completions`
  route, merged `615e7a4`). Prerequisite for S4 (the first scheduled Telegram brief = the Phase-3
  done-condition) and S5 (the operator console).
- **human_fork:** false. This is **infra** (compose YAML + a committed Hermes config template + an
  operator runbook). No data-shape lock-in, no deletion, nothing irreversible. The one architectural
  fork (how a separate-container Hermes keeps its model calls inside our sovereignty choke point) was
  already resolved with the user in S3a (ADR 0092). Reversal cost + revisit triggers recorded below.
- **Realizes:** ADR 0089 **D2** (Hermes runs as its own container connecting over HTTP+Zitadel-bearer)
  and **D3** (the LLM gateway is the sovereignty choke point) for the actual deployment topology.

## Context

S1 (ADR 0090) added an authenticated `streamable-http` transport to the MCP read server — exactly the
four read-only tools (`get_entity` / `get_neighbors` / `get_provenance` / `find_paths`), bearer-gated by
the same `ZitadelTokenVerifier` REST uses, fail-closed (it hard-raises at boot unless `ZITADEL_DOMAIN`
is configured). S3a (ADR 0092) added a thin OpenAI-compatible `POST /v1/chat/completions` route to the
existing FastAPI app that wraps the in-process S2 `LLMGateway` (so every model call still produces an
egress audit record and honours the confidential selector). Both halves of the surface Hermes consumes
now exist and are tested — **but nothing yet runs the MCP HTTP server, and Hermes has no compose home.**

S3b closes that. Two facts shape the design:

1. **The MCP HTTP server is not booted by anything today.** `deploy/compose.yaml` has no `mcp` service;
   `python -m worldmonitor.mcp` only runs HTTP when `MCP_TRANSPORT=streamable-http`, and it needs
   `ZITADEL_DOMAIN` + `ZITADEL_CLIENT_ID` (verifier audience) + `NEO4J_*` to serve the read tools.
2. **Hermes is `NousResearch/hermes-agent` v0.17.0, its own container, not vendored** (ADR 0089). It
   reaches our MCP over HTTP+bearer and its model over an OpenAI-compatible `base_url`. To keep its
   model traffic inside our egress audit + confidential selector (the sovereignty rule), that `base_url`
   must point at **our** S3a `/v1` route — never at Ollama/OpenRouter/Anthropic directly.

The dev box (WSL2) **cannot build Docker images** (broken apt proxy — see
`[[act-runs-github-actions-locally]]`) and there is no always-on host in this session, so S3b is **not
runtime-validatable here**. The gate is therefore an **infra-structural** one: `docker compose config`
validity (the existing `compose-boot` CI job) plus static structural checks. There is **no Python
invariant and no property test** in this slice — that class of gate does not apply to compose YAML +
a config template + docs.

## Decision

**Add an `mcp` service and a `hermes` service to `deploy/compose.yaml`, both under an opt-in `agent`
compose profile, plus a committed `deploy/hermes/config.yaml` template wiring Hermes' MCP→S1 and
model→S3a, plus an operator runbook `deploy/hermes/README.md`.** Concretely:

### 1. An `mcp` compose service (opt-in `agent` profile)
- `image: worldmonitor-app:dev` (the existing app image, `target: runtime` — tool-free per ADR 0077),
  `command: ["python", "-m", "worldmonitor.mcp"]` (the package `__main__` → `server.main()`).
- Env: `MCP_TRANSPORT=streamable-http`, `MCP_HTTP_HOST=0.0.0.0` (the default `127.0.0.1` would bind only
  loopback inside the container — unreachable by `hermes`), `MCP_HTTP_PORT=8765`,
  `ZITADEL_DOMAIN=${ZITADEL_DOMAIN:?...}`, `ZITADEL_CLIENT_ID=${ZITADEL_CLIENT_ID:?...}` (the verifier
  audience), optional `MCP_RESOURCE_SERVER_URL`, and `NEO4J_URI=bolt://neo4j:7687` / `NEO4J_USER=neo4j`
  / `NEO4J_PASSWORD=${NEO4J_PASSWORD}`.
- `networks: [default]` (reaches `neo4j:7687` for the read tools and `zitadel:8080` for JWKS),
  `depends_on:` neo4j **healthy** + zitadel **healthy**.
- `profiles: [agent]` — mirrors the `prometheus` service's `profiles: [monitoring]` pattern so the
  `compose-boot` job (which boots named core services, never `--profile agent`) never starts it.
- **No host port** in S3b (in-network only; `hermes` reaches `http://mcp:8765/mcp`). No healthcheck yet
  (revisit trigger below).

### 2. A `hermes` compose service (same opt-in `agent` profile)
- `image:` a **pinned external Hermes image ref** (`ghcr.io/nousresearch/hermes-agent:v0.17.0` if it is
  published; otherwise the runbook documents the operator builds it from the Nous repo on the host).
- `profiles: [agent]`, `networks: [default]` (reaches `mcp:8765` and `api:8000`),
  `depends_on:` `mcp` (started) + `api` (**healthy**).
- Mounts the committed `./hermes/config.yaml` **read-only** into the Hermes config dir
  (`${HERMES_HOME}/config.yaml:ro`, `HERMES_HOME` set to a known path; the operator confirms the exact
  default against the v0.17.0 docs).
- Env supplies the two service-principal bearers **only as `${...}` placeholders**:
  `WM_MCP_TOKEN=${WM_MCP_TOKEN:?...}` (the MCP bearer) and `WM_LLM_TOKEN=${WM_LLM_TOKEN:?...}` (the
  `/v1` bearer) — Hermes resolves them into `config.yaml` at connect time. **No secret is committed.**

### 3. `deploy/hermes/config.yaml` — the committed Hermes config template
- `mcp_servers.worldmonitor`: `url: "http://mcp:8765/mcp"`, `headers.Authorization: "Bearer
  ${WM_MCP_TOKEN}"`, `tools.include: [get_entity, get_neighbors, get_provenance, find_paths]`
  (**exactly** the four read tools — no write/active/enrich/score tool), `timeout`/`connect_timeout`,
  `ssl_verify: true`, `enabled: true`. (The streamable-http transport key + the `/mcp` mount path are
  confirmed against the v0.17.0 MCP-config reference in the spec.)
- `model`: `base_url: "http://api:8000/v1"` (**our** S3a route — never a provider URL),
  `api_key: "${WM_LLM_TOKEN}"`, `provider: custom`, a default model name.
- Secrets appear **only** as `${WM_MCP_TOKEN}` / `${WM_LLM_TOKEN}` placeholders, resolved by Hermes
  from the container env (which compose fills from the operator's host env). `${VAR}` in the config is
  resolved by **Hermes at connect time**, not by docker compose — so it never affects `compose config`.

### 4. `deploy/hermes/README.md` — the operator runbook
How to obtain/build the pinned Hermes image (the WSL box can't build — CI builds the app image; the
operator deploys on the always-on host); how to mint the two Zitadel service-principal bearers and grant
the `worldmonitor:graph-read` role to the `hermes` principal (the stub is in
`scripts/dev/zitadel_provision.sh`); the prerequisite that the existing `api` service has
`ZITADEL_DOMAIN`/`ZITADEL_CLIENT_ID` configured so the `/v1` bearer gate can verify; and the launch
command `docker compose -f deploy/compose.yaml --profile agent up -d`.

## The four S3b invariants (infra checks — not property tests; full checks in the gate spec)

- **INV-S3b-PROFILE — CI-safe opt-in.** Both `mcp` and `hermes` carry `profiles: [agent]`; the
  `compose-boot` job (no `--profile agent`) never boots them, and `docker compose -f deploy/compose.yaml
  config` stays valid. The `compose-boot` `.env` writer already auto-detects `${VAR:?}`/`${VAR}` and
  fills placeholders, so the new required vars (`WM_MCP_TOKEN`, `WM_LLM_TOKEN`, `ZITADEL_DOMAIN`,
  `ZITADEL_CLIENT_ID`) interpolate without any workflow edit.
- **INV-S3b-NOSECRET — no hardcoded secrets.** Every token/secret in the two services and in
  `config.yaml` is a `${VAR}` placeholder; nothing committed contains a real bearer/JWT/key.
- **INV-S3b-SOVEREIGNTY — model traffic stays inside the choke point.** Hermes' model `base_url` is
  `http://api:8000/v1` (our S3a route), not Ollama/OpenRouter/Anthropic; and `tools.include` is
  **exactly** the four read tools (no write/active tool).
- **INV-S3b-MCPBEARER — the MCP surface is bearer-gated.** Hermes sends `Authorization: Bearer
  ${WM_MCP_TOKEN}` and the `mcp` service runs the bearer-gated streamable-http transport with
  `ZITADEL_DOMAIN` set (so it does not hard-fail at boot and does not serve an anonymous port).

## Alternatives considered

- **Point Hermes' model directly at Ollama/OpenRouter/Anthropic.** Rejected — bypasses the S2 egress
  audit + confidential selector (the exact unaudited perimeter crossing ADR 0089 D3 / 0091 / 0092
  exist to prevent). `base_url` must be our `/v1` route.
- **Boot `mcp` (or `hermes`) as a default core service.** Rejected for S3b — `mcp` needs live Zitadel
  wiring and `hermes` needs live LLM/MCP + real bearers; booting either in `compose-boot` would fail or
  hang CI. The opt-in `agent` profile keeps CI green. (Promoting `mcp` to default-boot once it has a
  healthcheck is a recorded revisit trigger.)
- **Vendor Hermes into `src/`.** Rejected — ADR 0089 / CLAUDE.md: adopt/wrap external repos, never fork
  as foundation. Hermes stays its own pinned container.
- **A second `~/.hermes/.env` file holding the JWTs.** A valid operator choice (documented in the
  runbook), but the default wiring passes the bearers via compose `environment:` as `${...}` so there
  is one secrets source (the operator's host env), not two.
- **Co-located stdio MCP (no HTTP `mcp` service).** Rejected by ADR 0089 D2 — Hermes is remote; S1
  already built the HTTP transport for exactly this.

## Reversibility

- **Reversible (low cost):** delete the two services + `deploy/hermes/` and the new compose vars; the
  rest of the stack is untouched (no data-shape lock-in, no migration, no deletion of stored data).
  - **Revisit triggers:**
    - **Pinned Hermes image ref** — swap to an official published image (or a new tag) when one ships;
      until then the runbook documents build-from-source on the host. (Reversal: one-line image bump.)
    - **The opt-in `agent` profile** — once the topology is validated on the always-on host and the
      `mcp` service has a healthcheck, promote `mcp` to default-boot (drop its profile) so the read
      surface is always available; `hermes` stays opt-in (needs live bearers).
    - **The default model name** in `config.yaml` — operator-tunable; revisit when the selected local
      model changes.
- Reversible per CLAUDE.md build-discipline → **no human fork** beyond the Option-A choice already made
  with the user in S3a.

## Consequences

- The remote Hermes container has a compose home wired to both halves of the surface it consumes: the
  MCP read tools (S1) over HTTP+bearer and its model (S3a) through our sovereignty choke point.
- Two new services, both opt-in; no change to any core/booted service; **the `api` service is left
  untouched** (the runbook documents the operator's Zitadel-env prerequisite for the `/v1` bearer gate).
- **Not runtime-validatable on this dev box** (no image build, no always-on host) → this slice ships
  tagged **`scaffolded`/`implemented`**, validated by `compose-boot` config-interpolation + the static
  structural checks; runtime `operational` verification happens on the always-on host.
- Not person-affecting (read surface + service-side completion routing). External LLM modes still ship
  **off** (the S2 server-side default is Local/no-egress). Single-tenant (D1 / ADR 0042).

## References

- ADR 0089 (Phase-3 umbrella; D2 remote Hermes container / D3 LLM gateway choke point). ADR 0090 (S1 —
  the MCP HTTP+Zitadel-bearer transport this `mcp` service runs). ADR 0091 (S2 — the in-process LLM
  gateway). ADR 0092 (S3a — the `/v1/chat/completions` route Hermes' model points at). ADR 0063 (the
  four read-only MCP tools). ADR 0077 (the `target: runtime` tool-free image; the compose network
  isolation pattern). ADR 0078 (the `profiles: [monitoring]` opt-in pattern this mirrors). ADR 0042
  (single tenant). `docs/50_AGENT_LAYER.md` (Hermes division of labour, service-principal scoping).
  Memory `phase-3-hermes-decisions` (the HERMES CONFIG SURFACE block; S1/S2/S3a done-state).
  CLAUDE.md (data-sovereignty; adopt/wrap-never-fork; never hardcode secrets; reversibility
  classification).
</content>
</invoke>
