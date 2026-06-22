# 0033 — Neo4j bounded memory + pinned GDS-compatible image

- **Status:** accepted
- **Date:** 2026-06-22
- **Relates to:** [0002](README.md) (Neo4j + GDS = system of record), `docs/10_ARCHITECTURE.md` (bounded heap+pagecache), `docs/ARCHITECTURE_REVIEW.md` (the heap finding)

## Context

On a constrained always-on host (WSL2) the Neo4j container in `deploy/compose.yaml`
never bound `7687` — every resolve pass logged `connection refused`. Investigation
(grounded in CI evidence) found:

- **GDS is genuinely used.** `src/worldmonitor/graph/gds.py` calls `gds.graph.project`,
  `gds.degree.stream`, `gds.util.asNode`, `gds.graph.drop`; GDS is a locked decision
  (ADR 0002) and a roadmap deliverable. So GDS must stay — **pin, do not remove.**
- **The image + GDS pair is CI-proven.** `tests/conftest.py` uses the same
  `neo4j:2026.05.0-community` tag with the same `NEO4J_PLUGINS` auto-download, and CI
  runs `tests/integration/test_graph_gds.py` **green** (the full project→stream→drop
  round-trip). So a missing GDS build was *not* the cause.
- **The real defect: unbounded heap, uncapped container.** The service set only
  `pagecache_size`; with no `heap.max_size` and no container memory limit, the JVM
  auto-sizes max heap from visible RAM (~25%). On WSL2 (which reports a large fraction of
  host RAM) that over-commits and the OOM-killer reaps Neo4j before it binds — presenting
  as `connection refused`. CI passes only because the runner has ~16 GB.

## Decision

In `deploy/compose.yaml`'s `neo4j` service:

1. **Keep** the pinned `neo4j:2026.05.0-community` tag + `NEO4J_PLUGINS` + the `gds.*`
   procedure allowlist, with a comment that the pair is CI-proven and the tag must not be
   floated (the GDS jar is keyed to the exact server version).
2. **Bound the heap**: `NEO4J_server_memory_heap_initial__size` / `_heap_max__size`
   (note the doubled underscore Neo4j requires for a literal `_`), env-overridable.
3. **Cap the container**: `mem_limit: ${NEO4J_MEM_LIMIT:-4g}`, holding the invariant
   **heap_max + pagecache + ~1g overhead < mem_limit** (2g + 512m + ~1g = 3.5g < 4g).
4. Laptop/WSL2-safe defaults (heap 1–2 g, pagecache 512 m, limit 4 g) in `.env.example`,
   with a commented production line scaling toward `10_ARCHITECTURE.md`'s 16–24 GB.

## Consequences

- ✅ Neo4j stays inside its cgroup; the JVM can't OOM-kill itself on a constrained host.
- ✅ GDS capability (ADR 0002 / roadmap) is preserved; the CI-proven version pair is kept.
- ✅ Fully env-overridable — the same compose scales from WSL2 to the production host.
- Cannot be Docker-verified in the build environment; the reviewer gates the change and
  the operator's smoke run on a live stack is the runtime proof.
- If a host with filtered egress can't reach the GDS plugin download, that surfaces
  separately in `logs neo4j`; a mounted-jar fallback would be the follow-up (out of scope).
