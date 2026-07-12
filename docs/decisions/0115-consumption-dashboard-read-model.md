# 0115 — Consumption dashboard as a read-model; pause the storage-inversion lane

- **Status:** ACCEPTED (2026-07-12)
- **Date:** 2026-07-12
- **human_fork:** false — a reversible build-order and process decision. It writes no new
  system-of-record data, changes no ER/merge/erasure logic, and locks in no data shape. Reversal =
  stop extending the dashboard and resume the paused lane; the paused code is committed and its
  branch intact.
- **person_affecting:** false — the dashboard is a read-only projection over already-resolved,
  public-source data (news + sanctions). It does not change ER thresholds, merge decisions,
  individual-affecting scores, or erasure. The existing catastrophic-merge / sensitivity park and
  the person-affecting sign-off gates are untouched and still fire on the resolution path.

## Context

The build so far is a rigorous graph-native OSINT **backend**: of ~99 recorded ADRs, the large
majority are ER/resolution, storage, and safety plumbing; only a handful are user-facing (the
Integrations page, ADR 0069, and the read-only review queue, ADR 0103 slice 1a). The vision doc
(`docs/00_VISION_AND_SCOPE.md`) deliberately rejects the "dashboard" framing and sequences all rich
UI into Phase 6 (last). Two prior Fable strategic reviews (2026-07-04, 2026-07-11) independently
concluded the build order is inverted — *ship a consumption surface now* — and that advice was only
partly taken. The current in-flight work (the Gate 3b storage-inversion cutover + reconciliation +
write-path-integrity hardening, ADRs 0110–0114) is still deep backend substrate.

Net effect: **there is nothing a user can interact with**, and the heavy per-gate process
(gate-fleet + property tests + adversarial verify + ADR cosign) is what is consuming the time. The
operator's stated goal is the fastest, cheapest route to an interactive product comparable to
`koala73/worldmonitor` (live feeds → globe → AI briefs) — **with the resolved semantic graph kept
central** (the differentiator vs. that clone, which has no entity resolution or provenance) and
**without building on a bad foundation**.

The key enabling observation: the graph already works. The driver ingest loop → Splink resolution →
catastrophic-merge guard → Neo4j is real and CI-proven. An interactive dashboard does **not** need
the storage-inversion cutover finished — it reads Neo4j (the live SoR today, pre-cutover) via
bounded, guarded Cypher. So the dashboard is a standard read-model / projection that can be built in
parallel to (and independent of) the paused hardening lane.

## Decision

1. **Build the consumption dashboard as a decoupled read-model over the resolved graph.** A 3D
   globe + live feed rail + click-through entity relationship panel (with provenance receipts) +
   AI-synthesized briefs. It reads Neo4j via bounded Cypher reusing `graph/read_guards.py`; it
   **never writes the graph**. Delivered as thin vertical slices (seed & open-read → dashboard read
   API → globe SPA → semantic geo/event enrichment → AI briefs).
2. **Pause the storage-inversion lane** (Gate 3b cutover, reconciliation, WPI hardening). Neo4j
   stays the live SoR for the MVP. No code is deleted; the in-flight branch is left intact.
3. **Light process for the product lane.** Because the dashboard/read/ingest-glue code touches no
   person-affecting invariant, it ships with normal tests + one PR per slice + green CI +
   self-merge — **no** gate fleet, property tests, Opus judge, or ADR cosign. The cheap always-on
   invariants stay mandatory: provenance stamping on every new node/edge, canonical IDs, the SSRF
   guard on any new outbound fetch, and "leads not verdicts" for fuzzy geo/extraction. The
   resolution path's existing person-affecting gates remain fully in force.

## Status

ACCEPTED. Directed and authorized by the operator in-session (2026-07-12). Reversible +
non-person-affecting, so no cosign is required; recorded here per CLAUDE.md's reversibility rule.

## Consequences

- **Reversal cost:** low. Stop extending the dashboard; the read-model adds no SoR data to unwind.
  Resume the paused hardening lane from its intact branch. The public-read carve-out (ADR 0115,
  Slice A) is a config flip back to fully-gated.
- **Revisit trigger:** re-open the storage-inversion lane when (a) the MVP is demonstrable and the
  operator wants to resume graph-correctness hardening, or (b) Neo4j-as-SoR becomes a bottleneck
  (rebuild time, multi-store drift) the dashboard actually feels. Revisit the light-process posture
  the moment a product slice needs to touch an ER/merge/erasure path — that work moves back to the
  full gate.
- **Trade-off accepted:** the resolved graph's long-horizon correctness hardening slows while the
  product surface is built. Judged worth it: the graph is already load-bearing, and an undemonstrable
  product is the larger risk (both prior reviews' top finding).
- **Auth posture:** the dashboard read surface (`/api/dashboard/*`, the `/app` SPA, `/static`) is
  public on a single-tenant, self-hosted deploy serving public-source data; the write/operator
  surface (Integrations, review, `/v1/chat/completions`) stays Zitadel-gated.
