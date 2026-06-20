# WorldMonitor

A self-hosted, **graph-native, ontology-first, plugin-extensible** OSINT / geopolitical
intelligence platform. Many sources → one canonical, provenance-tracked entity graph →
analysis on top → exposed via an **API + MCP surface** → driven by a self-improving agent layer.

The **resolved entity graph is the product.** See the plan in [`docs/`](docs/README.md), starting
with [`docs/00_VISION_AND_SCOPE.md`](docs/00_VISION_AND_SCOPE.md) and
[`docs/10_ARCHITECTURE.md`](docs/10_ARCHITECTURE.md). Agent ground rules live in
[`CLAUDE.md`](CLAUDE.md).

## Status

**Phase 0 — Foundations** (see [`docs/40_ROADMAP.md`](docs/40_ROADMAP.md)): a clean, reproducible,
secure, auth-gated skeleton.

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
cp .env.example .env                                          # fill in values first
docker compose -f deploy/compose.yaml --env-file .env up -d   # core services
./scripts/dev/zitadel_provision.sh                            # create the OIDC apps
```

Brings up the core services (Neo4j+GDS, PostgreSQL+pgvector, MinIO, Redis, Zitadel).
The provisioning script creates the `worldmonitor-api` and `hermes` OIDC apps and
prints the `ZITADEL_DOMAIN` / `ZITADEL_CLIENT_ID` to paste back into `.env`.
(`--env-file .env` is required because the compose file lives under `deploy/`.)
