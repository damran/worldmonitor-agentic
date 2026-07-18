# WorldMonitor

A self-hosted, **graph-native, ontology-first, plugin-extensible** OSINT / geopolitical
intelligence platform. Many sources → one canonical, provenance-tracked entity graph →
analysis on top → exposed via an **API + MCP surface** → driven by a self-improving agent layer.

The **resolved entity graph is the product.** See the plan in [`docs/`](docs/README.md), starting
with [`docs/00_VISION_AND_SCOPE.md`](docs/00_VISION_AND_SCOPE.md) and
[`docs/10_ARCHITECTURE.md`](docs/10_ARCHITECTURE.md). Agent ground rules live in
[`CLAUDE.md`](CLAUDE.md).

## Status

See [`docs/40_ROADMAP.md`](docs/40_ROADMAP.md) for the authoritative milestone state. As of 2026-07:

- **Phase 1 (spine) and Phase 2 (API/MCP surface + Integrations UI + live/stream connectors) are
  complete** — `connector → FtM ontology → Splink ER (merge-guarded) → Neo4j graph → REST/MCP reads`
  is real and CI-proven (ADRs 0060–0072).
- **Consumption dashboard MVP shipped** (ADR 0115): a public, read-only product at **`/app`** — 3D
  globe of geo-located events, live feed rail, click-through entity panel with provenance receipts,
  entity search, and AI-synthesized briefs with citations. It is a bounded read-model over the
  resolved graph; it never writes it.
- **Phase 3 (Hermes agent layer) infrastructure shipped** (ADRs 0089–0093); operational deployment
  on the always-on host awaits operator verification (`docs/runbooks/OPERATOR_SESSION.md`).
- **Storage inversion (Postgres statement log = SoR, Neo4j = derived projection, ADR 0095)** is
  built and dormant; the Gate 3b cutover is deliberately paused pre-cutover (ADRs 0113–0115) and
  remains human-gated.

## Quickstart (dashboard)

```bash
uv run python scripts/dev/gen_env.py    # generates .env with strong random secrets
docker compose -f deploy/compose.yaml --env-file .env up -d
# then open http://localhost:8000/app
```

News→event extraction and AI briefs additionally need Ollama reachable on the host and
`EXTRACTION_ENABLED=true` (see `docs/runbooks/OPERATOR_SESSION.md`); the globe, feed, search, and
entity panel work without any LLM.

## Development

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync                 # create venv + install deps (incl. dev group)
uv run pytest           # tests + coverage
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pyright          # type-check (strict on src/)
uv run pre-commit install   # enable git hooks
```

### Local stack

```bash
uv run python scripts/dev/gen_env.py    # generates .env with strong random secrets
docker compose -f deploy/compose.yaml --env-file .env up -d   # core services
./scripts/dev/zitadel_provision.sh                            # create the OIDC apps
```

Brings up the core services (Neo4j+GDS, PostgreSQL+pgvector, MinIO, Redis, Zitadel).
The provisioning script creates the `worldmonitor-api` and `hermes` OIDC apps and
prints the `ZITADEL_DOMAIN` / `ZITADEL_CLIENT_ID` to paste back into `.env`.
(`--env-file .env` is required because the compose file lives under `deploy/`.)
