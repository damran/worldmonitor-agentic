# ADR 0086 — Landing-zone GC safety hardening (review remediation)

- **Status:** ACCEPTED
- **Gate:** Gate B — landing-zone GC safety (`docs/reviews/GATE_B_LANDING_GC_SAFETY_SPEC.md`).
- **Addresses:** adversarial review 2026-06-29 of PRs #138–145 — three **medium** findings against
  the landing-zone orphan GC (ADR 0083 / PR #143). 0 critical / 0 high. Default-off ⇒ no behaviour
  change for any current deployment.
- **Touches:** the G1 provenance invariant (landing bytes are raw-pointer provenance). Per CLAUDE.md
  build discipline this gate adds a mandatory `@given` property/metamorphic suite.
- **Supersedes nothing.** Extends ADR 0083 (which stays Accepted); the reference-based,
  report-only-by-default model is unchanged.

## Context

ADR 0083 added a reference-based GC for landing-zone orphans. Three latent hazards surfaced in
review:

1. **Grace window vs. ingest timeout.** `run_ingest` does `landing.put(key, …)` BEFORE the windowed
   `session.commit()` that creates the referencing `er_queue` row. The put→commit gap is bounded by
   `ingest_timeout_seconds`. If `landing_gc_min_age_seconds < ingest_timeout_seconds` (or `0`) while
   deletion is enabled, a GC pass can delete the put-before-commit object of an ingest that is *still
   in flight* — provenance destroyed for a record about to be committed. Nothing currently prevents
   that configuration.

2. **Reference set omits Neo4j.** "Referenced" is `ErQueueItem.source_record ∪
   IngestDeadLetter.source_record` only — no Neo4j provenance pointers. This is safe **only because
   `er_queue` rows are never hard-deleted** (a resolved/processed candidate's `source_record` persists
   forever). That invariant was undocumented and unguarded.

3. **`GcStats.bytes_freed` is misnamed.** It is bytes of orphan *candidates identified*, computed even
   in report-only / dry-run mode — not bytes *freed*. The Prometheus gauge already uses the right noun
   (`worldmonitor_landing_orphan_bytes`); the struct field disagrees.

## Decision

### D1 — Grace-window guard: fail-closed at config validation (reversible)

Add a `Settings` `@model_validator(mode="after")` that, **only when `landing_gc_delete_enabled is
True`**, rejects an unsafe grace window:

- `landing_gc_min_age_seconds == 0` → `ValueError` (no grace + deletion is unsafe).
- `ingest_timeout_seconds == 0` (deadline disabled ⇒ unbounded in-flight window) → `ValueError`: no
  finite grace is provably safe; set a finite `ingest_timeout_seconds` or disable deletion.
- `0 < landing_gc_min_age_seconds < ingest_timeout_seconds` → `ValueError` naming both values.
- otherwise OK (`min_age >= timeout > 0`, boundary `==` allowed). Report-only mode (`delete` off) is
  unconstrained — it is purely read.

**Fail-closed, not clamp.** A silent clamp (raise the effective floor to `ingest_timeout_seconds`)
would hide the misconfiguration: an operator who wrote `min_age=60, timeout=1800` has a wrong mental
model and should be corrected at boot, not handed `1800` quietly. Config-validation refusal is the
same discipline as ADR 0061/0068 secret validation and the ADR 0047 abstain-band validator already in
`settings.py`. The cost of a wrong delete here is irreversible (provenance gone); the cost of a strict
validator is a one-line config fix — asymmetric, so we fail closed.

- **Classify (reversibility): reversible.** It is config-validation behaviour, no data shape, nothing
  public-facing. **Reversal cost: low** — switch the `raise` to a clamp-with-`logger.warning`, or
  relax the rule, in one validator. **Revisit trigger:** an operator reports a legitimate
  short-grace / bounded-timeout deployment the validator wrongly refuses, OR we adopt per-connector
  timeouts (then the single global `ingest_timeout_seconds` is no longer the right bound and the rule
  must be re-derived).

### D2 — Reference-set invariant: document + guard, do NOT union Neo4j now (reversible)

Keep the reference set as `er_queue ∪ ingest_dead_letter`. Make the load-bearing dependency explicit:

- A code comment at the reference-set build in `gc.py` naming **ER-QUEUE-NEVER-HARD-DELETED**: the
  `ErQueueItem` reference query is UNFILTERED (all rows, all statuses); omitting Neo4j provenance
  pointers is safe **only** while `er_queue` rows are never hard-deleted. If a hard-delete is ever
  added, the GC MUST also union Neo4j `prov_source_id` pointers.
- A test guard: `test_reference_set_covers_all_er_statuses` fails if a status `WHERE` filter is ever
  added to the ER reference query (rows of every status must all count as referenced).
- The mandatory `@given` suite (`tests/property/test_prop_landing_gc_reference_safety.py`): **P-REF**
  (a referenced object is never an orphan candidate, any age), **P-MM-MONOTONE** (enlarging the
  reference set never creates a candidate), **P-ER-STATUS** (an object referenced only by a
  resolved/processed ER row is never selected). To make these pure decisions, the classification loop
  is extracted into a pure `select_orphan_candidates(objects, referenced_uris, *, now,
  min_age_seconds)` helper (no behaviour change).

We do **not** union Neo4j provenance pointers now: the invariant holds today, the union would add a
per-pass graph query to a default-off maintenance path, and a node's `prov_source_id` is not
guaranteed to be the same `s3://` URI string (a separate mapping, out of scope). Documenting + guarding
the existing invariant is the proportionate fix.

- **Classify (reversibility): reversible.** **Reversal cost: medium** — if `er_queue` ever gains a
  hard-delete, add a Neo4j `prov_source_id` union to the reference set (one extra query + URI
  normalisation). **Revisit trigger:** any PR that introduces an `er_queue` hard-delete / TTL, or a
  status-filtered reference query — the guard test is designed to fail loudly at exactly that moment.

### D3 — Rename `GcStats.bytes_freed → orphan_bytes` (reversible)

Rename the field to match what it measures and the existing gauge noun. Ripple through `gc.py`,
`collector.py` (the `gc.orphan_bytes` read), `driver.py` (import only), and tests. The Prometheus
gauge **name** `worldmonitor_landing_orphan_bytes` is unchanged (already correct), so the ADR 0078
alert-rules parity test is unaffected.

- **Classify (reversibility): reversible** (internal field name; no schema, no metric-name, no API
  change). **Reversal cost: trivial.** **Revisit trigger:** none expected.

## Consequences

- No schema change; not person-affecting; default-off (`landing_gc_enabled=False`,
  `landing_gc_delete_enabled=False`) ⇒ no behaviour change until an operator opts in. The only
  observable change for a current deployment is that a future *delete-enabled* config with an unsafe
  grace now fails to boot (the intended safety).
- The pure-classifier extraction (D2) is a no-op refactor — the live path computes the identical
  candidate set.
- Gauge surface unchanged (D3 keeps the name); alert-rules parity test passes untouched.

## Alternatives considered

- **Clamp the grace window instead of failing closed (D1).** Rejected: hides misconfiguration; the
  cost asymmetry (irreversible delete vs. one-line config fix) favours fail-closed. Recorded as the
  reversal path if the strict rule proves too rigid.
- **Union Neo4j provenance pointers into the reference set now (D2).** Rejected as premature: adds a
  graph query to a default-off path and a URI-normalisation surface, for an invariant that holds
  today. Kept as the documented reversal if `er_queue` hard-delete ever lands.
- **Leave `bytes_freed` (D3).** Rejected: actively misleading in report-only mode, the GC's default.

## Tests

- **Unit (`tests/unit/test_landing_gc.py`):** grace-guard accept/reject matrix (5 cases, D1);
  `test_reference_set_covers_all_er_statuses` (D2 guard); rename assertions (D3).
- **Property (`tests/property/test_prop_landing_gc_reference_safety.py`):** P-REF, P-MM-MONOTONE,
  P-ER-STATUS at `max_examples >= 150`, `deadline=None` (D2 — mandatory `@given`).
- **Regression:** existing `tests/integration/test_landing_gc.py` (MinIO) and the ADR 0078
  alert-rules parity test stay green (gauge name unchanged).
