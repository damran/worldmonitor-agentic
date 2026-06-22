# 0034 — Neo4j compose auth fix + a compose-boot CI guard

- **Status:** accepted
- **Date:** 2026-06-22
- **Follows:** [0033](0033-neo4j-bounded-memory.md) (the previous deploy-config fix)

## Context

The Neo4j container crash-looped and never bound `7687` — not heap/GDS (the [0033](0033-neo4j-bounded-memory.md)
suspects), but a **config-validation** failure. `deploy/compose.yaml` passed a bare
`NEO4J_PASSWORD: ${NEO4J_PASSWORD}` to the neo4j service. The Neo4j image maps **every**
`NEO4J_<x>` env var to a `neo4j.conf` setting, so `NEO4J_PASSWORD` became an invalid
`password` setting that Neo4j 2026's **strict config validation** rejects → the server
exits on startup → restart-loop → `connection refused`.

This is the **second** deploy-config defect (after the unbounded heap) that the existing
CI missed, for the same structural reason: **CI exercises the app against *testcontainers*,
never against `deploy/compose.yaml`.** Testcontainers' `Neo4jContainer` sets `NEO4J_AUTH`
itself and never passes a bare `NEO4J_PASSWORD`, so the compose defect was invisible.

## Decision

1. **Set the Neo4j password via `NEO4J_AUTH` only.** Remove the bare
   `NEO4J_PASSWORD` from the neo4j service; keep `NEO4J_AUTH: neo4j/${NEO4J_PASSWORD}`.
   The neo4j service now carries **only** valid `NEO4J_`-prefixed settings (AUTH, plugins,
   the `gds.*` allowlist, heap/pagecache). `NEO4J_PASSWORD` stays in `.env` for the app
   **client**; the **server** must not inherit it.
2. **Fix the healthcheck** to interpolate the password at compose-parse time
   (`${NEO4J_PASSWORD}`) instead of expanding the now-absent container env var
   (`$$NEO4J_PASSWORD`).
3. **Add a `compose-boot` CI job** (`.github/workflows/compose-boot.yml`): write a `.env`
   covering every compose variable, `docker compose config` (interpolation check), then
   `up -d --wait` the core services and require Neo4j to pass its healthcheck (Bolt up +
   auth working) within a timeout, dumping `logs neo4j` on failure and tearing down. This
   runs the **real deploy config** on a runner, so this class of defect fails CI going
   forward.

## Consequences

- ✅ Neo4j boots: only valid settings reach `neo4j.conf`, strict validation passes.
- ✅ The blind spot is closed — `deploy/compose.yaml` is now boot-tested in CI, not just
  the testcontainers stack. A future bad `NEO4J_*` var, a missing required env var, or a
  config that won't come up will fail `compose-boot`.
- The job pulls images + auto-downloads GDS on each run (a few minutes); acceptable for a
  deploy-config gate. It boots only the core app services (postgres/neo4j/minio), not the
  heavier redis/zitadel.
- Runtime proof remains the operator's smoke run on the live stack.
