# 0124 — `summary` context-budget flag on `get_neighbors` / `find_paths` (REST + MCP)

- **Status:** PROPOSED (flips to ACCEPTED at the gate-completing PR — the 0117–0123 convention)
- **Date:** 2026-07-24
- **human_fork:** false — a reversible, additive, opt-in, read-only **shaping** of two existing
  list-returning graph reads. No product/architecture fork. Default is a sensible one (flag off, full list
  unchanged); the two scoping calls (union return annotation vs. a schema-neutral list-of-one; in-helper
  sort vs. query `ORDER BY`) each have a cheap reversal and a recorded revisit trigger (below). Not marked
  OPEN.
- **person_affecting:** false — see "Person-affecting reasoning" below. The flag makes **no** change to the
  live system (no ER threshold, guard mode, sensitivity park, score, or model/param promotion), performs
  **no** inference/scoring/attribution, has **zero** egress, and writes **nothing** (no table, no
  migration, no graph write). It surfaces a **subset** of what the same read already returns — *less* data,
  never more — with per-record provenance intact.
- **human_cosign:** not required — reversible, non-person-affecting, read-only shaping (per the cost
  directive: reserve cosign for irreversible / person-affecting changes).
- **Backlog/roadmap:** `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-5** (P1 / S, one gate).
  F-10 (JMESPath projection) is queued **behind** F-5 and is a hard non-goal here.
- **Spec:** `docs/reviews/GATE_F5_SUMMARY_FLAG_SPEC.md`.
- **Builds on:** ADR 0062 (REST read routes), 0063 (stdio MCP + shared `read_guards`), 0064 (read caps +
  the **no-`ORDER BY`** decision), 0090 (authenticated HTTP MCP), 0121 (F-2: MCP annotations + typed
  output schemas + `{error, hint}`), 0122 (F-3: shared-helper REST↔MCP lockstep + parity precedent), 0042
  (single-tenant), 0095 (statement log = SoR; Neo4j = derived projection).

## Context

An agent caller (Hermes) that calls `get_neighbors` on a high-degree hub node receives up to
`NEIGHBOR_RESULT_LIMIT` (500) full neighbour dicts, or up to `PATH_RESULT_LIMIT` (50) paths from
`find_paths` — a large context payload it often does not need in full when it is only orienting ("how many,
and a taste"). The backlog asks for an opt-in **context-budget flag** returning `{count, sample[3]}`, with
a **shared helper so REST + MCP stay lockstep**.

Three facts (empirically verified against the installed `mcp==1.28.1` and FastAPI; see the spec §1) shape
the decision:

1. **MCP `structured_output=True` derives one `outputSchema` from one static return annotation.** A summary
   return is a **dict** while normal mode returns a **list** — a shape collision. A dict returned from a
   `list[dict]`-annotated tool raises a validation `ToolError` (`convert_result` validates against the
   list output-model). So the tool's return annotation must admit both shapes.
2. **A union return annotation `list[dict[str, Any]] | dict[str, Any]` is byte-transparent in normal
   mode.** A list return still yields N content blocks (byte-identical) and `structuredContent =
   {"result":[…]}`; only the static `outputSchema` changes (`result: {type: array}` →
   `result: {anyOf:[array, object]}`). A dict return yields one content block = the bare `{count, sample}`.
3. **FastAPI uses the return annotation as the response_model.** Returning `{count, sample}` from a route
   annotated `dict[str, list[dict[str, Any]]]` is a 500; widening to `dict[str, Any]` serialises both the
   unchanged `{"neighbors":[…]}` and the new `{count, sample}`.

Separately, the underlying query order is **non-deterministic today**: `get_neighbors`/`find_paths` carry a
`LIMIT` with **no `ORDER BY`** (ADR 0064 deferred ordering as a sort-cost enhancement). So there is no
"existing deterministic order" to take a first-3 from; determinism must be established at the summary layer.

## Decision

Add an opt-in `summary` flag (REST query param `?summary=true`; MCP tool arg `summary: bool = False`) to
`get_neighbors` and `find_paths` on **both** surfaces, shaped by **one** shared, pure helper.

1. **One shared helper — `graph/queries.py::summarize_result(items, *, sample_size=3) -> dict[str, Any]`**
   returning `{"count": len(items), "sample": <deterministic first ≤ sample_size>}`. Pure (no client, no
   `Session`, no Cypher, no I/O). Both surfaces call it on the list they already fetched, so REST and MCP
   are byte-identical by construction (the F-3 lockstep convention; pinned by a REST↔MCP parity test for
   each tool). The existing `get_neighbors`/`find_paths` **Cypher is not touched**.

2. **Determinism via an in-helper canonical sort, NOT a query `ORDER BY`.** `sample = sorted(items,
   key=lambda d: json.dumps(d, sort_keys=True, default=str))[:sample_size]`. A field-agnostic total order
   over any JSON dict → one helper serves both neighbours and paths. For any result **at/below** the
   existing cap (the common case) the full set is deterministic, so the sample is fully deterministic; a
   `>`cap hub inherits ADR 0064's arbitrary-bounded-subset residual (F-5 adds **no** new non-determinism).

3. **MCP: a union return annotation** `list[dict[str, Any]] | dict[str, Any]` on the two tools (and their
   `_register_read_tools` closures). This preserves **every normal-mode byte** (N content blocks,
   `structuredContent = {"result":[…]}`) and changes **only** the static `outputSchema` (`result` gains an
   `anyOf` admitting the `{count, sample}` object) — additive contract evolution. Summary-mode
   `structuredContent` is `{"result": {count, sample}}` (the SDK wraps the dict); the summary content block
   is the bare `{count, sample}`.

4. **REST: widen the two routes' return annotation** `dict[str, list[dict[str, Any]]]` → `dict[str, Any]`
   (required for FastAPI response serialisation of `{count, sample}`; the normal `{"neighbors":[…]}` /
   `{"paths":[…]}` body is byte-identical).

5. **`sample_size` is fixed at 3** (the backlog literal `sample[3]`) as the helper's default arg — not a
   new `read_guards` constant, not configurable in v1.

6. **The dossier stays full-fat.** The flag lives at the **surface** layer (`read_neighbors` /
   `tool_get_neighbors`), not inside the `get_neighbors` query helper, so `get_entity_dossier` (which calls
   the helper directly, without the flag) is unaffected.

Exactly **one** existing test pin changes: the `get_neighbors`/`find_paths` branch of
`test_all_tools_have_output_schema` (relax `result.type == "array"` to admit the `anyOf` array branch) — a
sanctioned test-author edit (spec §6.1). Every other pin (all F-2 PP-1 content-parity, the five-tool set,
`outputSchema is not None`, the REST normal-mode envelopes, both MCP property tests) stays green.

## Alternatives considered

- **MCP: return the summary as a list-of-one `[{count, sample}]`, keeping the `list[dict]` annotation
  (zero outputSchema change).** Breaks **no** MCP pin, but the two surfaces then return *different* summary
  payloads (REST `{count, sample}` vs MCP `[{count, sample}]`) — sacrificing the backlog's "REST + MCP stay
  lockstep" and muddying the shared-helper parity. Rejected: lockstep is the row's headline; the cost of
  the chosen path is one relaxed schema assertion.
- **MCP: "list truncated + a count".** A count cannot ride a bare list. Rejected as incoherent.
- **MCP: a divergent arg (e.g. `limit`) instead of `summary`.** Would give REST and MCP different contracts
  — again breaks lockstep. Rejected.
- **Determinism via `ORDER BY` in the shared query (the task's fallback).** Reopens ADR 0064's deliberately
  deferred sort-cost decision, changes normal-mode truncation *selection* for `>`cap hubs (a behaviour
  change beyond F-5), and enlarges a P1/S gate. The in-helper sort delivers the needed determinism at a
  fraction of the blast radius. Deferred behind the revisit trigger.
- **Put `summary` inside the `get_neighbors` query helper.** Would make the dossier inherit a union-shaped
  neighbours section and reshape a helper the dossier depends on. Rejected: keep the flag at the surface,
  leave the query helpers and the dossier untouched.
- **A projected/sub-selected `sample` (fewer fields).** That is F-10 (JMESPath projection) — a separate
  gate, and it would launder provenance out of the sample. Out of scope; `sample` elements are the full
  dicts.

## Consequences

- Agent/REST callers can request `{count, sample[3]}` to bound context on hub reads; the full,
  provenance-complete list remains the default (flag off) and is byte-identical to today.
- The two MCP tools' `outputSchema.result` becomes an `anyOf[array, object]` (a superset still admitting
  the array). Clients that only read `structuredContent` see `{"result":[…]}` unchanged in normal mode and
  `{"result":{count,sample}}` in summary mode.
- The two REST routes' response_model widens to `dict[str, Any]`; the normal body is unchanged. No OpenAPI
  drift check exists yet (F-7 unbuilt), so no CI/doc update is forced.
- **G1 is not weakened:** `sample` carries each element's `prov_*` verbatim; the flag reduces cardinality
  (like ADR 0064's `LIMIT`), never per-record provenance. The dossier is unchanged. **No migration, no new
  datastore, no write, no egress. Single-tenant.**

## Person-affecting reasoning

A neighbour/path may be a `Person`, but the flag exposes a **subset** of the exact records
`get_neighbors`/`find_paths` already return — never a new field, never more data, and never a provenance
strip (each `sample` element keeps its `prov_*`). It changes no ER threshold, guard mode, sensitivity park,
score, or model/param promotion; it performs no inference, attribution, or resolution; it writes nothing
and egresses nothing. It is read-only shaping of an already-bounded, already-authorised read. Hence
person_affecting = false and no cosign is required.

## Reversibility

Reversible. Reversal cost: **low** — remove the `summary` param and the `summarize_result` call, revert the
two return annotations (MCP union → `list`; REST `dict[str, Any]` → the narrow form), and restore the one
relaxed schema assertion. No data written, so nothing to migrate back. Revisit triggers: (a) callers need a
*deterministic global* top-N (not "first-3 of the bounded set") or pagination → add `ORDER BY` + keyset
pagination to the query (the ADR 0064 enhancement); (b) `sample_size` needs tuning → promote it to a
`read_guards` constant / setting; (c) a true unbounded degree is wanted → a separate `count(*)` query; (d)
the dossier wants compact neighbours → adopt `summarize_result` there (F-3's own revisit trigger).

## Invariant gate note

F-5 touches **no** CLAUDE.md invariant (no provenance-stamping change, no ER/merge/threshold, no
canonical-id, no guard, no write) — it is read-only shaping surfacing *less* data. So a `@given` is **not**
mandated by build-discipline. We nonetheless include **one** cheap metamorphic property test
(`test_prop_summary_count_consistency`): `summarize_result(x)["count"] == len(x)`, `sample ⊆ x`,
`len(sample) == min(3, len(x))`, and determinism under input reordering — pinning the load-bearing
guarantee that the summary can never disagree with the full list. Recorded as a **decision, not an
omission** (mirroring ADR 0121 §3.1 / ADR 0122 §3.5).
