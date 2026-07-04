# 0096 — Tagging & labels as first-class, provenance-carrying statements

- **Status:** ACCEPTED (2026-07-04) — user-flagged as **"one of our main points"** and confirmed as
  a first-class capability that the design had under-specified. This ADR makes tagging first-class and
  shows it is a *consequence of* the storage model (ADR 0095) + the overlay primitive (`docs/70` §2),
  not a bolt-on.
- **Date:** 2026-07-04
- **Realises:** the operator's requirement that information be **tagged wherever possible so
  tag-search materially helps investigation**. Extends `docs/20_ONTOLOGY.md` and `docs/70` (v0.2 adds
  the UI). Depends on ADR 0095 (statement log), composes with the review/sign-off discipline
  (ADR 0031/0047) and leads-not-verdicts (C5).

## Context

The UI design (`docs/70` v0.1) and the review had provenance, canonical anchors, ontology types,
confidence, and sensitivity classes — but **no first-class analyst/information tagging** and no
tag-search as a cross-cutting primitive. For an OSINT/CTI platform this is a real gap: tagging and
taxonomies are how analysts organise, triage, and retrieve (MISP taxonomies/galaxies, STIX `labels`,
TheHive tags, Aleph tags). The operator flagged it directly.

The key realisation: **a tag is a claim.** In the ADR-0095 statement model, a tag is just a statement
with a tag predicate — so tagging inherits the platform's hardest-won properties for free, rather than
needing a parallel mechanism.

## Decision

**A tag is a provenance-carrying statement.** Model every tag as:

```
statement(subject, predicate = wm:tag | <namespace>:<key>, value = <tag value>,
          dataset/source = analyst:uid | scorer@version | connector,
          asserted_at, retracted_at, valid_from?, valid_to?,
          reliability, confidence, sensitivity_class?)
```

From this shape, tagging inherits — **by construction, not by discipline**:

- **Provenance & audit.** Every tag records *who/what applied it, when, and how reliably*. "Why is
  this entity tagged `sanctioned`?" resolves to the source claim. Critical for an intelligence tool.
- **Bitemporality.** *When* something was tagged (`asserted_at`), whether a tag was retracted, and
  time-bounded tags (`valid_from/to`). "What was tagged `high-risk` as of last month" is a query.
- **Leads, not verdicts (C5).** An **auto-tag** from a scorer/classifier carries a `confidence` — it
  is a *lead*, rendered as a confidence-bearing chip, never a verdict. A **person-affecting auto-tag**
  (a risk label on a natural person) is **sign-off-gated** exactly like an ER merge — it routes
  through the review queue, is human-reviewable, and is never an automated accusation.
- **GDPR/erasure.** A tag about a person *is* personal data; because it is a statement, erasure,
  rectification, and the audit trail already cover it (ADR 0049) — no separate tag-erasure path.
- **Searchable through the existing surfaces.** Tag search is a query over the statement
  log/projection; a tag facet is a **Selection**; a tag filter is an **Overlay** (`docs/70` §2). So
  "everything tagged X" composes onto the map / graph / dashboard / watchlist like any other overlay,
  and rides the same query surface (pipe-DSL `search tag:sanctioned` / Cypher).

### Kinds of tags (a small taxonomy *of* tags)

1. **Ontology labels** — controlled vocabulary already in the schema: STIX `labels`, FtM topics,
   sensitivity classes. Validated against the L2 contract.
2. **Analyst tags** — free-form or controlled tags an analyst applies to entities / claims / cases /
   leads (`source = analyst:uid`, high reliability). Global or case-scoped.
3. **Auto-tags** — applied by enrichers / scorers / rules (`source = scorer@version`, carry
   `confidence`). Leads. E.g. `pep`, `coordinated-behaviour`, `typosquat`, `high-taint`. Person-
   affecting ones are sign-off-gated.
4. **System tags** — reliability grade, source, status (`researched → operational`), provenance
   density. Already structured; exposed as searchable facets.

### Controlled taxonomies (namespaced tags)

Support **namespaced controlled vocabularies** — `tlp:amber`, `admiralty:reliability=B`,
`misp-galaxy:threat-actor="Lattice"`, STIX `labels` — as first-class tag namespaces. Free tags are
allowed but the UI nudges toward controlled vocab for retrievability. **A tag namespace is a small
ontology extension** (data, not code) governed by the same `wm:` extension rule (`docs/20` §4,
ADR-per-namespace where it warrants one) — so adding a taxonomy is a pack, not a code change.

### Non-destructive tag management

Rename / merge / deprecate a tag is **non-destructive**: a rename is a new claim + supersession of the
old (provenance retained), consistent with the ADR-0095 append-only model. No tag history is lost.

### Query, index, API

- A **tag index** (a materialised tag → entities projection, rebuildable from the statement log) gives
  fast faceted "tag search" — the capability the operator asked for.
- Tags are queryable in **both** the pipe-DSL (`| where tag = "…"`) and Cypher, and exposed through the
  read API + MCP so **Hermes can search and filter by tags** (read, now). *Applying* a tag from the
  agent is a write tool → Phase 6, gated.

## Alternatives considered

- **A separate tag table / free-text label column.** Rejected — it would be a parallel model without
  provenance, bitemporality, confidence, or erasure; exactly what the statement model gives for free.
  (Also violates the ADR-0095 "one truth" intent.)
- **Tags as bare Neo4j node properties.** Rejected — same loss of provenance/lineage as the flat
  `prov_*` problem the storage inversion fixes; tags-as-properties can't record *who tagged what when*.
- **Free-form tags only (no taxonomies).** Rejected as the *only* mode — free tags fragment search;
  controlled namespaces are what make tag-search reliable. Free tags remain allowed alongside.

## Consequences

- Tagging is a first-class, cross-cutting retrieval + triage layer that strengthens (not bypasses) the
  provenance, audit, and leads-not-verdicts invariants.
- `docs/70` v0.2 adds the UI (tag chips with source-on-hover + confidence dot; faceted tag search;
  tag-as-overlay; bulk tagging in the workbench; tag-driven watchlist alerts; a non-destructive tag/
  taxonomy manager).
- Person-affecting auto-tags are held to the same sign-off gate as ER merges — a new place the human
  budget (ADR 0094 D6) is spent, sized by the same abstention/degrade-conservative logic.
- Implemented in the tagging gate of the execution plan (`docs/fable-review/70_EXECUTION_HANDOFF.md`),
  after the statement spine (its substrate) and the review-queue UI (its sign-off surface).

## Reversibility

Reversible in mechanism (tag predicates + index are additive to the statement log), but tagging is a
committed *capability*. Revisit trigger: if controlled-taxonomy governance proves heavier than its
retrieval value, fall back to analyst-free-tags-only (still statements) — the data model is unchanged.
