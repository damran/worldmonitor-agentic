# Gate S3b — Deploy the Hermes agent layer + the MCP HTTP server (compose) — BUILD SPEC

> ADR: `docs/decisions/0093-hermes-deploy.md`. Umbrella: ADR 0089 (D2/D3). Phase-3.
> This is an **INFRA gate** (compose YAML + a Hermes config template + an operator runbook).
> **No Python invariant and no property test apply.** The gate is: `docker compose config` validity
> (the existing `compose-boot` CI job) + the four static structural checks in §4. Do **not** add a
> `tests/property/` test for this slice.

## 0. One-paragraph goal

Give the remote Hermes container (`NousResearch/hermes-agent` v0.17.0, ADR 0089) a compose home, and
boot the MCP HTTP read server (S1, ADR 0090) that Hermes consumes. Both as **opt-in** compose services
(profile `agent`) so the `compose-boot` CI job never starts them. Wire Hermes' MCP at our bearer-gated
`http://mcp:8765/mcp` (the four read tools only) and its model at our S3a `http://api:8000/v1` route (so
its model calls stay inside the egress audit + confidential selector). Secrets are `${VAR}` placeholders
only. Ship `scaffolded`/`implemented` — not runtime-validatable on the WSL dev box (no image build, no
always-on host); `compose-boot` validates config interpolation.

## 1. Exact files in scope (and why)

| File | Change |
|---|---|
| `deploy/compose.yaml` | Add two services — `mcp` and `hermes` — both under `profiles: [agent]`. Leave **every existing service untouched** (esp. `api`). |
| `deploy/hermes/config.yaml` | **New.** The committed Hermes config template: MCP→S1 (4 read tools, bearer) + model→S3a `/v1` (bearer). Secrets as `${WM_MCP_TOKEN}`/`${WM_LLM_TOKEN}` only. |
| `deploy/hermes/README.md` | **New.** Operator runbook: build/pull the pinned Hermes image; mint the two Zitadel service-principal bearers + grant `worldmonitor:graph-read`; the `api` Zitadel-env prerequisite; `--profile agent up`. |
| `docs/decisions/0093-hermes-deploy.md` | The ADR (already drafted alongside this spec). |
| `docs/reviews/GATE_S3B_HERMES_DEPLOY_SPEC.md` | This spec. |

**Out of scope / do NOT touch:** all of `src/`; the frozen S1/S2/S3a modules; the `api`/`driver`/
`sandbox-runner`/store services in compose; `.github/workflows/compose-boot.yml` (its `.env` writer
already auto-detects `${VAR:?}`/`${VAR}` and fills placeholders — **no workflow edit is needed**; if a
builder thinks it is, STOP — that signals a non-detected var form was used, which is the bug).

## 2. Verified facts to build against (do NOT re-investigate)

- **MCP server invocation:** `python -m worldmonitor.mcp` (the package `__main__.py` calls
  `server.main()`). **Not** `python -m worldmonitor.mcp.server`.
- **MCP HTTP needs (from `mcp/server.py::main` + `settings.py`):**
  - `MCP_TRANSPORT=streamable-http` (default `stdio`).
  - `MCP_HTTP_HOST` — default `127.0.0.1`; **must be set to `0.0.0.0`** or the port binds loopback-only
    inside the container and `hermes` can't reach it.
  - `MCP_HTTP_PORT` — default `8765`.
  - `ZITADEL_DOMAIN` — **required**; `main()` hard-raises `RuntimeError` at boot if unset
    (`mcp_transport=streamable-http requires zitadel_domain ...`). Fail-closed by design.
  - `ZITADEL_CLIENT_ID` — the verifier **audience** (`settings.zitadel_client_id`); required for tokens
    to verify.
  - `MCP_RESOURCE_SERVER_URL` — optional (RFC 9728 metadata); empty → omitted.
  - `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` — the read tools query Neo4j.
- **MCP mount path:** `streamable_http_app()` mounts at `/mcp` → Hermes URL `http://mcp:8765/mcp`.
- **S3a route:** `POST /v1/chat/completions` on the existing `api` service (`:8000`) → Hermes model
  `base_url: http://api:8000/v1`.
- **The four read tools (exact names):** `get_entity`, `get_neighbors`, `get_provenance`, `find_paths`.
- **App image:** `worldmonitor-app:dev`, build `target: runtime` (tool-free, ADR 0077). The `mcp`
  service reuses this image — no new Dockerfile.
- **Opt-in profile pattern:** the `prometheus` service uses `profiles: [monitoring]` (compose.yaml
  ~L426); a bare `up` / the `compose-boot` `up -d <named services>` never starts it. Mirror with
  `profiles: [agent]`.
- **compose-boot `.env` writer:** `.github/workflows/compose-boot.yml` regex-detects
  `${VAR:?...}` and bare `${VAR}` and writes `ci-unused` placeholders (special-casing only
  `NEO4J_PASSWORD` + `CONFIG_ENCRYPTION_KEY`). New required vars in `${VAR:?}`/`${VAR}` form are picked
  up automatically. A `${VAR:-default}` form is **not** detected (don't use it for the new secrets).
- **Hermes config surface (v0.17.0, from the memory HERMES CONFIG SURFACE block):**
  `mcp_servers.<name>` carries `url`, `headers.Authorization: "Bearer ${VAR}"`, `tools.include`
  allowlist, `timeout`/`connect_timeout`, `ssl_verify`, `enabled`. `${VAR}` placeholders resolve at
  connect time from env. The streamable-http transport key + the `/mcp` path are confirmed against
  `hermes-agent.nousresearch.com/docs/reference/mcp-config-reference`; model→OpenAI-compatible endpoint
  via `base_url` + `api_key` (Bearer when `base_url` set).
- **Zitadel:** `scripts/dev/zitadel_provision.sh` already creates the `hermes` OIDC app and the
  `worldmonitor:graph-read` role (read + run-passive) with `projectRoleAssertion=true`. The runbook
  references this; **do not edit the script in S3b** (it is frozen S1 surface).
- **Hermes image:** no confirmed published image. Use a **pinned** ref
  (`ghcr.io/nousresearch/hermes-agent:v0.17.0`) with a comment that the operator confirms/pulls it or
  builds from the Nous repo on the host (the WSL box can't build). Reversible (ADR 0093 revisit trigger).

## 3. The design to build

### 3.1 `mcp` service (in `deploy/compose.yaml`)
- `image: worldmonitor-app:dev`, `command: ["python", "-m", "worldmonitor.mcp"]`.
- `profiles: [agent]`, `restart: unless-stopped`, `networks: [default]`.
- `depends_on:` `neo4j: {condition: service_healthy}`, `zitadel: {condition: service_healthy}`.
- `environment:` `MCP_TRANSPORT: streamable-http`, `MCP_HTTP_HOST: 0.0.0.0`, `MCP_HTTP_PORT: "8765"`,
  `ZITADEL_DOMAIN: ${ZITADEL_DOMAIN:?set ZITADEL_DOMAIN in .env (MCP HTTP bearer auth)}`,
  `ZITADEL_CLIENT_ID: ${ZITADEL_CLIENT_ID:?set ZITADEL_CLIENT_ID in .env}`,
  `MCP_RESOURCE_SERVER_URL: ${MCP_RESOURCE_SERVER_URL:-}` (optional),
  `NEO4J_URI: bolt://neo4j:7687`, `NEO4J_USER: neo4j`, `NEO4J_PASSWORD: ${NEO4J_PASSWORD}`.
- **No host port** (in-network only). **No healthcheck** in S3b (the streamable-http app has no
  confirmed health route; adding one + promoting to default-boot is the ADR 0093 revisit trigger).
- A header comment mirroring the `prometheus` comment: why opt-in, how it boots, why no host port.

### 3.2 `hermes` service (in `deploy/compose.yaml`)
- `image: ghcr.io/nousresearch/hermes-agent:v0.17.0` (pinned; comment: confirm/pull or build on host).
- `profiles: [agent]`, `restart: unless-stopped`, `networks: [default]`.
- `depends_on:` `mcp: {condition: service_started}`, `api: {condition: service_healthy}`.
- `environment:` `WM_MCP_TOKEN: ${WM_MCP_TOKEN:?set WM_MCP_TOKEN in .env (Hermes MCP bearer)}`,
  `WM_LLM_TOKEN: ${WM_LLM_TOKEN:?set WM_LLM_TOKEN in .env (Hermes /v1 bearer)}`,
  `HERMES_HOME: ${HERMES_HOME:-/etc/hermes}`.
- `volumes:` `./hermes/config.yaml:${HERMES_HOME:-/etc/hermes}/config.yaml:ro` (read-only mount of the
  committed template; the operator confirms the exact default config path against the v0.17.0 docs).
- `command:` the long-running Hermes process (e.g. `["hermes", "gateway", "run"]`) — confirm the exact
  subcommand against the v0.17.0 CLI docs; a wrong command does not affect the config-validity gate but
  fix it for the runbook. Reversible.

### 3.3 `deploy/hermes/config.yaml` (committed template)
```yaml
# WorldMonitor — Hermes agent config template (Phase-3 S3b, ADR 0093).
# Secrets are ${VAR} placeholders ONLY — Hermes resolves them from the container env at connect time
# (compose fills them from the operator's host env). NEVER commit a real bearer/JWT here.
mcp_servers:
  worldmonitor:
    url: "http://mcp:8765/mcp"            # our bearer-gated streamable-http MCP (S1, ADR 0090)
    transport: streamable_http            # confirm exact key vs v0.17.0 mcp-config-reference
    headers:
      Authorization: "Bearer ${WM_MCP_TOKEN}"
    tools:
      include: [get_entity, get_neighbors, get_provenance, find_paths]   # EXACTLY the 4 read tools
    timeout: 300
    connect_timeout: 60
    ssl_verify: true
    enabled: true
model:
  default: "<local-model-name>"           # operator-set; resolved backend is decided server-side (S3a)
  provider: custom
  base_url: "http://api:8000/v1"          # OUR S3a route — NOT Ollama/OpenRouter/Anthropic (sovereignty)
  api_key: "${WM_LLM_TOKEN}"
```
(The builder may adjust key names/nesting to match the exact v0.17.0 schema — but the four invariant
values in §4 must hold verbatim: the MCP URL host:port/path, the `Bearer ${WM_MCP_TOKEN}` header, the
exact four-tool `include` list, and the `http://api:8000/v1` model `base_url`.)

### 3.4 `deploy/hermes/README.md` (operator runbook)
Cover: (a) build/pull the pinned Hermes image (CI builds the app image; the WSL box can't build — deploy
on the always-on host); (b) run `scripts/dev/zitadel_provision.sh`, then grant `worldmonitor:graph-read`
to the `hermes` principal and mint the two service-principal bearers (`WM_MCP_TOKEN` for MCP,
`WM_LLM_TOKEN` for `/v1`); (c) **prerequisite:** the existing `api` service must have
`ZITADEL_DOMAIN`/`ZITADEL_CLIENT_ID` configured so the `/v1` `get_principal` bearer gate can verify (S3b
does not modify the `api` service); (d) set the env vars in the host `.env`; (e) launch:
`docker compose -f deploy/compose.yaml --profile agent up -d`; (f) note token acquisition/refresh beyond
documenting it is out of scope (S4/S5).

## 4. The four infra invariants — concrete checks the checker runs

> These are **structural/static** checks. No property test.

**INV-S3b-PROFILE — CI-safe opt-in (the load-bearing one).**
- `docker compose -f deploy/compose.yaml config >/dev/null` succeeds with the `compose-boot` `.env`
  (i.e. with `WM_MCP_TOKEN`/`WM_LLM_TOKEN`/`ZITADEL_DOMAIN`/`ZITADEL_CLIENT_ID` set to placeholders).
  Locally: write a throwaway `.env` setting those four (+ the existing required vars) to any string, then
  run the `config` command — it must parse.
- `docker compose -f deploy/compose.yaml config --services` lists `mcp` and `hermes` **only** when they
  are in the profile set; with no profile, `docker compose -f deploy/compose.yaml config --profiles`
  shows `agent` and the default service list does **not** auto-include `mcp`/`hermes`.
  Concretely: `docker compose -f deploy/compose.yaml --profile agent config --services` includes both;
  the `compose-boot` `up -d <named core services>` (no `--profile agent`) cannot start them.
- The real proof: the `compose-boot` CI job stays green (it never names `mcp`/`hermes` and never passes
  `--profile agent`).

**INV-S3b-NOSECRET — no hardcoded secrets.**
- Grep the two new services + `deploy/hermes/config.yaml` for any literal bearer/JWT/key: every secret
  is a `${VAR}` placeholder. No `eyJ`-prefixed JWT, no inline password, no real `api_key`. The only
  secret-bearing tokens are `${WM_MCP_TOKEN}` and `${WM_LLM_TOKEN}` (+ `${NEO4J_PASSWORD}` reused).

**INV-S3b-SOVEREIGNTY — model traffic stays inside the choke point.**
- In `config.yaml` the model `base_url` is exactly `http://api:8000/v1` — **not** an Ollama/OpenRouter/
  Anthropic URL. Grep proves no `ollama`/`openrouter`/`anthropic`/`openai.com` host in the model block.
- `tools.include` is **exactly** `[get_entity, get_neighbors, get_provenance, find_paths]` — no fifth
  entry, no write/active/enrich/score/run_connector tool.

**INV-S3b-MCPBEARER — the MCP surface is bearer-gated.**
- The `hermes` MCP `headers.Authorization` is `"Bearer ${WM_MCP_TOKEN}"`.
- The `mcp` service sets `MCP_TRANSPORT=streamable-http` **and** `ZITADEL_DOMAIN` (so `main()` does not
  hard-raise and does not serve an anonymous port), `MCP_HTTP_HOST=0.0.0.0`, `MCP_HTTP_PORT=8765`.

## 5. How to validate locally (the WSL box — no image build needed)

```bash
# from repo root; throwaway env so `config` can interpolate the new required vars
printf 'NEO4J_PASSWORD=x\nPOSTGRES_PASSWORD=x\nMINIO_ROOT_PASSWORD=x\nREDIS_PASSWORD=x\n'\
'ZITADEL_MASTERKEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\nZITADEL_ADMIN_PASSWORD=x\n'\
'CONFIG_ENCRYPTION_KEY=x\nSANDBOX_RUNNER_SECRET=x\n'\
'WM_MCP_TOKEN=x\nWM_LLM_TOKEN=x\nZITADEL_DOMAIN=localhost\nZITADEL_CLIENT_ID=x\n' > /tmp/s3b.env

docker compose --env-file /tmp/s3b.env -f deploy/compose.yaml config >/dev/null   # must succeed
docker compose --env-file /tmp/s3b.env -f deploy/compose.yaml --profile agent config --services  # lists mcp, hermes
docker compose --env-file /tmp/s3b.env -f deploy/compose.yaml config --services  # must NOT list mcp/hermes

# no hardcoded secrets (no JWT, no inline key) in the new surfaces:
grep -nE 'eyJ|api_key:\s*["'"'"']?[A-Za-z0-9._-]{20,}' deploy/hermes/config.yaml deploy/compose.yaml || echo "clean"
```
(If `docker compose` isn't available on the box, `python -c "import yaml,glob; [yaml.safe_load(open(f)) for f in ['deploy/hermes/config.yaml']]"` at least proves the template is valid YAML; the authoritative `config` check runs in `compose-boot` CI.)

## 6. Slice breakdown (1-3 builder slices)

- **Slice 1 — the `mcp` compose service.** Add the `mcp` service (§3.1) under `profiles: [agent]`.
  Individually mergeable. Check: `docker compose config` valid; `--profile agent config --services`
  shows `mcp`; default service list does not; `compose-boot` green. No new file.
- **Slice 2 — the `hermes` service + `deploy/hermes/config.yaml`.** Add the `hermes` service (§3.2) +
  the config template (§3.3). **Merge after Slice 1** (its `depends_on: mcp` needs the `mcp` service to
  exist for `config` to validate). Check: all four §4 invariants; `compose-boot` green.
- **Slice 3 — `deploy/hermes/README.md` operator runbook.** §3.4. Fully independent (docs-only),
  mergeable any time. No code/config check beyond markdown sanity.

## 7. Out of scope (explicit)

- S4: the first scheduled Telegram brief (Hermes cron → "what changed about entity X" → Telegram) — the
  Phase-3 done-condition. Not here.
- S5: the operator console chat UI + the first SSE endpoint + runtime operator mode-flip.
- Runtime token acquisition/refresh logic beyond **documenting** it in the runbook.
- Any change to `src/`, the `api`/`driver`/store services, `scripts/dev/zitadel_provision.sh`, or
  `.github/workflows/compose-boot.yml`.
- Active/write MCP tools (enrich/run_connector/resolve) — Phase 6, human-gated (ADR 0089).
- A property/invariant test — does not apply to this infra gate (see header).

## 8. Done = the gate is green when

`docker compose -f deploy/compose.yaml config` parses with the new required vars; `mcp` + `hermes` are
present **only** under `--profile agent`; the four §4 invariants hold (no hardcoded secret; model
`base_url` == `http://api:8000/v1`; `tools.include` == the four read tools; MCP `Bearer ${WM_MCP_TOKEN}`
+ `mcp` runs streamable-http with `ZITADEL_DOMAIN`); the `compose-boot` CI job is green; the runbook
documents image/bearer/launch. Tag the slice `scaffolded`/`implemented` (runtime `operational`
verification is on the always-on host, per ADR 0093).
</content>
