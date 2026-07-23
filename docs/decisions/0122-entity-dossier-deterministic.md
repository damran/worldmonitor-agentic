# 0122 — `get_entity_dossier` (slice 1): deterministic aggregation over the existing read helpers

- **Status:** ACCEPTED (2026-07-23)
- **Date:** 2026-07-23
- **human_fork:** false — a reversible, additive read-only aggregation surface over already-exposed reads;
  no product/architecture fork. The two scoping calls (merge history = recorded absence; no
  `assembled_at`) each have a sensible default, a cheap reversal, and a revisit trigger (below). This
  ADR is therefore **not** marked OPEN.
- **person_affecting:** false — see the honest calculus in "Person-affecting reasoning" below. Slice 1
  exposes **no new data**: every field is already individually retrievable by the same authorized caller
  via the shipped `get_entity` / `get_neighbors` / `get_provenance` tools+routes. It makes **no** change
  to the live system (no ER threshold, guard mode, sensitivity park, score, or model/param promotion),
  performs **no** inference/scoring/attribution, and has **zero egress** (no LLM). The dossier **always**
  carries provenance (enforced by a mandatory `@given`), so it is the opposite of a laundering surface.
  The person-affecting weight lives in **slice 2** (LLM narrative + egress audit) — a separate future
  gate.
- **human_cosign:** not required — reversible, non-person-affecting, non-ER-adjacent read surface (per the
  cost directive: reserve cosign for irreversible / person-affecting changes). The mild "mosaic effect"
  consideration is recorded below so a human may veto at ADR-accept time; it does not rise to a fork.
- **Backlog/roadmap:** `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-3** (P1 / M, 2 gates —
  **this is slice 1 only**, deterministic, zero LLM).
- **Spec:** `docs/reviews/GATE_F3_ENTITY_DOSSIER_SPEC.md`.
- **Builds on:** ADR 0062 (REST read routes), 0063 (stdio MCP), 0064 (read caps), 0090 (authenticated
  HTTP MCP), 0121 / Gate F-2 (MCP contract polish), 0042 (single-tenant), 0095 (statement/decision log =
  SoR; Neo4j = derived projection).

## Context

Backlog row F-3 asks for a `get_entity_dossier` brief tool, split into two gates: **slice 1 =
deterministic assembly** (entity + neighbors + provenance + merge history from existing query helpers,
zero LLM) and slice 2 = a gateway-only LLM narrative. This ADR governs **slice 1 only**.

We already expose the resolved graph over four read-only surfaces, each backed by a helper in
`graph/queries.py` and shared by REST (ADR 0062) and MCP (ADR 0063/0090/0121): `get_entity`,
`get_neighbors`, `get_provenance`, `find_paths`. A dossier is the natural composition of the first three
into one call — a convenience for Hermes and, later, the `wm` CLI.

**The merge-history finding (verified):** the row's "merge history from existing query helpers" is
optimistic. There is a merge audit trail, but it is **not** in `graph/queries.py` and **not**
graph-readable — it lives in **Postgres** and its readers need a SQLAlchemy `Session`:
`merge_audit` (`db/models.py::MergeAudit`, read via `select(MergeAudit).where(canonical_id == …)` as in
`api/review.py` / `resolution/signoff.py`) and the append-only `canonical_id_ledger`
(`resolution/canonical.resolve_durable`). Neo4j nodes carry `prov_*` and the per-property witness map
(`prov_witnesses` — *which sources witnessed which property*) but **not** the merge lineage (*which source
ids collapsed into this canonical*). The REST surface has a session (`api/deps.get_db`); the **stdio MCP
server has none** (`build_server` takes only a `Neo4jClient`, and its trust boundary / 12-factor story is
Neo4j-only, ADR 0063). Wiring Postgres into the stdio MCP server is **new plumbing** that would break the
"one shared graph-only helper both surfaces call" lockstep and expand an XS/S gate.

## Decision

Ship **one** deterministic aggregation endpoint on **both** surfaces, assembled by **one shared helper**.

**D1 — One shared assembly helper (lockstep).** Add
`graph/queries.py::get_entity_dossier(client, *, entity_id, hops=1) -> dict | None`, composing **only**
the three existing helpers (no new Cypher, no write, no `Session`). The REST route
(`GET /entities/{entity_id}/dossier`) and the MCP tool (`get_entity_dossier`) are thin pass-throughs of
this one helper (the F-5 "shared helper so REST + MCP stay lockstep" convention); a byte-parity test pins
that they never drift.

**D2 — Fixed, deterministic response shape, no free text.**
`{ entity, neighbors, provenance, merge_history }` — every section traceable to a query helper. `entity`
= `get_entity` (node props incl. `prov_*`); `neighbors` = `get_neighbors` (bounded by
`NEIGHBOR_RESULT_LIMIT` + `HOP_CAP`, ADR 0064; 1-hop default); `provenance` = `get_provenance` (the node's
`prov_*` map). Absent entity → helper returns `None` → REST 404 / MCP `{error, hint}` (mirrors the
existing surfaces). **No `assembled_at`** wall-clock stamp (non-deterministic, not graph-traceable;
additive-safe to add later). **No free text** (that is slice 2).

**D3 — Merge history is a RECORDED ABSENCE this slice.** The schema always carries a `merge_history` key
with a fixed, machine-readable sentinel `{"status": "not_assembled", "available": false}` — an explicit
absence, not a silent omission, and not prose. Rationale: the trail is Postgres-only and the MCP surface
has no `Session`; a Session-taking helper would be un-satisfiable by stdio MCP without new plumbing that
breaks lockstep and blows the gate. A **present** key lets consumers rely on later population without a
breaking shape change.

**D4 — A fifth MCP tool + a new REST route, under the F-2 conventions.** The tool goes 4 → 5, deliberately
breaking F-2's PP-3 "exactly four" pin. Every hardcoded tool count/set (the spec §6.1 enumerates all
eleven loci, incl. `deploy/hermes/config.yaml`) is updated by the **test-author** — the sanctioned
test-edit — to the five-tool set. The new tool registers in the single shared `_register_read_tools` with
`ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)`, `title`,
`structured_output=True`, and the raise-based `{error, hint}` envelope (reusing the `"entity not found"`
token) — mirroring `server.py`'s now-standard shape so stdio and HTTP stay identical.

**D5 — Mandatory `@given` provenance-surface test.** Unlike F-2 (no invariant touched → no property test),
F-3 is a new data-exposure surface that touches **provenance exposure**: the dossier must never present an
entity without its provenance. A property test asserts that for any present entity the assembled dossier
carries a non-empty `provenance` section and an `entity` section that includes every `prov_*` key — the
surface analogue of G1.

This ADR flips to **ACCEPTED** at the gate-completing PR (the 0117-0121 convention).

## Person-affecting reasoning (recorded either way)

The dossier **aggregates** data about an entity that may be a **Person**. Does exposing an *aggregation
view of already-exposed reads* change the person-affecting calculus? Reasoned out:

1. **No new data.** Every field is already individually retrievable by the same authorized caller via the
   shipped `get_entity`/`get_neighbors`/`get_provenance` tools+routes. The dossier is a mechanical
   composition (3 reads in 1 call) — no new field, query, inference, score, ranking, or attribution.
2. **Same gate, same audience.** Same `get_principal` / bearer auth, same single-tenant boundary (D1, ADR
   0042). It neither lowers the auth bar nor widens who can see the data.
3. **No change to the live system.** CLAUDE.md's person-affecting sign-off gates cover *changes* affecting
   a real person (ER thresholds, individual-affecting scores, model/param promotion). A read view persists
   nothing, decides nothing, mutates no threshold/guard/score — it is outside the self-improvement gate
   entirely.
4. **Zero egress.** No LLM, no external transmission. The person-affecting-heavy surface (LLM narrative +
   egress audit + CTI `framework` param) is **slice 2** — deliberately deferred.
5. **Provenance always attached.** The dossier always carries the provenance section and the entity's
   `prov_*` (enforced by D5) — the opposite of laundering; a consumer always sees where each fact came
   from (GDPR/audit posture preserved).
6. **No linkage disclosure this slice.** `merge_history` is a recorded absence (D3), so the dossier does
   **not** expose which identities were merged/linked — the most sensitive linkage-decision data is not
   surfaced in slice 1.

**Honest counter-consideration — the mosaic effect.** Aggregation can raise privacy salience even when
each field is individually available: a one-call profile of a Person is qualitatively a "dossier."
Recorded explicitly. Mitigations: the mosaic is already trivially assemblable by any authorized caller (3
calls) — the gate neither lowers auth nor widens audience; the payload is bounded
(`NEIGHBOR_RESULT_LIMIT`/`HOP_CAP`); provenance is always attached; there is no egress; and no
merge/linkage is disclosed (D3).

**Conclusion:** slice 1 is **not** person-affecting in the CLAUDE.md sense. **Revisit the analysis** if a
future slice (i) adds a field **not** already individually exposed, (ii) removes/weakens the provenance
section, (iii) adds egress/LLM (slice 2 — which carries its own person-affecting weight + egress audit),
or (iv) **populates `merge_history`** (assess whether exposing the merge/linkage of a Person's identities
needs its own review).

## Alternatives considered

- **A1 — Populate `merge_history` from Postgres now.** Rejected: needs a `Session` in the shared helper,
  which the **stdio MCP surface lacks** — wiring Postgres into the stdio MCP server is new plumbing that
  breaks the graph-only lockstep (D1) and expands the gate. Chose the recorded absence (D3) + a revisit
  trigger.
- **A2 — REST-only `merge_history` (REST has a session, MCP omits it).** Rejected: the two surfaces would
  return different bodies, breaking the lockstep parity guarantee (D1) and the parity test — the whole
  point of the shared helper.
- **A3 — Include an `assembled_at` wall-clock stamp.** Rejected: non-deterministic, not traceable to a
  query helper, and it would break byte-parity. Additive-safe to add later if a consumer needs it.
- **A4 — Omit the `merge_history` key entirely.** Rejected: a present sentinel lets consumers rely on the
  field and its later population without a breaking shape change; silent omission is what CLAUDE.md's
  provenance/audit posture discourages.
- **A5 — Typed per-field Pydantic response model.** Deferred to F-7 (OpenAPI artifact); keep
  `dict[str, Any]` consistent with the existing routes/tools (no payload reshape).
- **A6 — Fold the dossier into the slice-2 LLM gate.** Rejected: the backlog explicitly splits the
  deterministic slice from the LLM slice; slice 1 must ship value with zero egress and zero
  person-affecting weight.

## Reversibility

**Reversible** (`human_fork` = false, `person_affecting` = false).

- **Reversal cost:** revert one helper (`graph/queries.py`), one REST route (`api/graph.py`), the MCP tool
  registration (`mcp/server.py` back to four), the eleven test/config pin updates, and the Hermes
  include-list. **No** data migration, **no** schema/store change, **no** new table, **no** stored
  artifacts. The one lock-in is the response **shape** (a semi-public contract for Hermes/CLI/MCP hosts) —
  but it is a **new additive surface with zero locked-in consumers yet**, auth-gated single-tenant (not
  anonymous public), and deliberately additive-friendly (populating `merge_history` or adding a field
  later is backward-compatible; only renaming/removing a top-level key is breaking).
- **Revisit triggers:**
  - (a) **Merge-history availability** — when the MCP surface gains a DB-session context (or we accept a
    REST-only enrichment), populate `merge_history` from `resolution.canonical.resolve_durable`
    (superseded-id aliases) + the `merge_audit` decision trail. Re-run the person-affecting analysis for
    linkage disclosure (person-affecting note (iv)).
  - (b) **F-5 `summary` context-budget flag** reshapes `get_neighbors` → the dossier's `neighbors` section
    adopts the shared summary helper.
  - (c) **F-7 OpenAPI artifact** → introduce a typed response model (A5).
  - (d) **Slice 2 (LLM narrative)** → a separate gate with the egress audit + CTI `framework` param and
    its own person-affecting assessment.
  - (e) A consumer needs `assembled_at` → add it additively (A3).

## Consequences

- Hermes (and the later `wm` CLI) get an entity workup in one call instead of three; the payload is
  deterministic, bounded, and always provenance-carrying.
- The five-tool MCP surface stays lockstep across stdio/HTTP (one registration site) and lockstep with
  REST (one assembly helper); the parity test pins both.
- The dossier deliberately does not yet surface merge lineage (recorded absence); consumers can rely on
  the `merge_history` key existing and later becoming populated without a breaking change.
- No CLAUDE.md invariant is weakened; the new provenance-surface invariant is added and enforced by a
  mandatory `@given` (unlike F-2, which recorded none).
