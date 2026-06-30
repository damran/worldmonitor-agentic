# Hermes agent — operator runbook (Phase-3 S3b, ADR 0093)

> Status: `scaffolded`/`implemented` — runtime `operational` verification is on the always-on host.
> This runbook covers first-time setup. Token acquisition/refresh beyond initial minting is out of scope (S4/S5).

## Prerequisites

- The core WorldMonitor stack is running: `postgres`, `neo4j`, `minio`, `redis`, `zitadel`, `api`, `driver`.
- `ZITADEL_DOMAIN` and `ZITADEL_CLIENT_ID` are set in the host `.env` (see §3 below).
- `NEO4J_PASSWORD` and all other core `.env` vars are already configured.

---

## 1. Obtain the Hermes image

The WSL2 dev box cannot build Docker images (broken apt proxy). Deploy on the always-on host.

```bash
# Option A — pull the pinned image if it has been published to GHCR:
docker pull ghcr.io/nousresearch/hermes-agent:v0.17.0

# Option B — build from source (if the image is not published):
#   git clone https://github.com/NousResearch/hermes-agent /tmp/hermes-agent
#   cd /tmp/hermes-agent && git checkout v0.17.0
#   docker build -t ghcr.io/nousresearch/hermes-agent:v0.17.0 .
```

Confirm the exact image ref + default config path (`HERMES_HOME`) against the v0.17.0 release notes
before first boot. The `HERMES_HOME` env var defaults to `/etc/hermes` in `compose.yaml`; adjust if the
v0.17.0 image uses a different config directory.

---

## 2. Provision Zitadel — run the provisioning script

```bash
# Run once (or re-run safely; script is idempotent):
bash scripts/dev/zitadel_provision.sh
```

This creates:
- The `worldmonitor` API/resource server.
- The `hermes` service-principal OIDC application.
- The `worldmonitor:graph-read` role (read + run-passive).

After the script completes, in the Zitadel admin console:
1. **Grant `worldmonitor:graph-read`** to the `hermes` service principal so its tokens carry the role.
2. **Mint two long-lived service-principal bearers** (Zitadel: Service Users → Personal Access Tokens or
   machine-to-machine client-credentials flow):
   - `WM_MCP_TOKEN` — the Hermes→MCP bearer (verified by the `mcp` service's `ZitadelTokenVerifier`).
   - `WM_LLM_TOKEN` — the Hermes→`/v1` bearer (verified by the `api` service's bearer gate).

---

## 3. Wire the `api` service — Zitadel env prerequisite

The `api` service's `/v1/chat/completions` bearer gate (S3a, ADR 0092) verifies Hermes' `WM_LLM_TOKEN`
against Zitadel. The verifier is constructed lazily (JWKS fetched on first token verification, not at
boot), so `ZITADEL_DOMAIN` and `ZITADEL_CLIENT_ID` are **optional for core boot** — both default to
empty string (auth unconfigured, verifier not built) when unset.

For the agent profile to work, these MUST be set in your host `.env`:

```dotenv
ZITADEL_DOMAIN=<your-zitadel-domain>     # e.g. auth.example.com
ZITADEL_CLIENT_ID=<client-id>            # the worldmonitor API client ID from the Zitadel console
```

The `api` service in `deploy/compose.yaml` already declares `ZITADEL_DOMAIN: ${ZITADEL_DOMAIN:-}` and
`ZITADEL_CLIENT_ID: ${ZITADEL_CLIENT_ID:-}`, so simply setting these in the host `.env` is sufficient —
no compose file edit is required.

---

## 4. Set all required vars in the host `.env`

Add these to your `deploy/.env` (or the root `.env` the compose file reads):

```dotenv
# --- Phase-3 S3b: agent profile ---
ZITADEL_DOMAIN=<your-zitadel-domain>
ZITADEL_CLIENT_ID=<worldmonitor-api-client-id>
WM_MCP_TOKEN=<hermes-mcp-bearer>          # minted in §2; Hermes → mcp:8765/mcp
WM_LLM_TOKEN=<hermes-llm-bearer>          # minted in §2; Hermes → api:8000/v1

# Optional — override Hermes config dir if the v0.17.0 image differs from the default:
# HERMES_HOME=/etc/hermes

# Optional — for RFC 9728 protected-resource metadata on the MCP server:
# MCP_RESOURCE_SERVER_URL=https://mcp.example.com
```

The `config.yaml` template at `deploy/hermes/config.yaml` is mounted read-only into the Hermes
container. Hermes resolves the `${WM_MCP_TOKEN}` and `${WM_LLM_TOKEN}` placeholders from the container
env at connect time — **no real token is committed to the repo**.

---

## 5. Launch the agent profile

```bash
# Start the core stack first (if not already running):
docker compose -f deploy/compose.yaml up -d

# Then bring up the agent services:
docker compose -f deploy/compose.yaml --profile agent up -d

# Check status:
docker compose -f deploy/compose.yaml ps
```

The `--profile agent` starts `mcp` and `hermes` only. The core services (`postgres`, `neo4j`, etc.)
continue to run without `--profile agent`.

To stop only the agent services:
```bash
docker compose -f deploy/compose.yaml --profile agent down
```

---

## 6. Verify connectivity

```bash
# MCP server log — should show "Serving streamable-http on 0.0.0.0:8765":
docker compose -f deploy/compose.yaml logs mcp

# Hermes log — should show connection to mcp:8765/mcp and api:8000/v1:
docker compose -f deploy/compose.yaml logs hermes
```

---

## 7. Notes and revisit triggers (ADR 0093)

- **Pinned Hermes image ref** — swap to an official published image (or a new tag) when one ships.
  Until then, build from source on the host (see §1 Option B). Reversal: one-line image bump.
- **The opt-in `agent` profile** — once the topology is validated on the always-on host and the `mcp`
  service has a confirmed health route, promote `mcp` to default-boot (drop its profile) so the read
  surface is always available; `hermes` stays opt-in (needs live bearers).
- **The default model name** in `config.yaml` (`<local-model-name>`) is a placeholder — set the
  operator's preferred local model. The resolved backend is decided server-side by the S3a LLM gateway
  (S2, ADR 0091); the name here is passed through as Hermes' default model hint.
- **Token acquisition/refresh** — documenting initial minting is this runbook's scope. Automated
  refresh (client-credentials flow, short-lived tokens, rotation) is Phase-4/5 (S4/S5).
- **`HERMES_HOME`** — confirm the exact default config directory against the v0.17.0 Hermes docs.
  If the image uses a different path, set `HERMES_HOME` in the host `.env`.
