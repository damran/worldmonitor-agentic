# ADR 0083 — Landing-zone orphan GC (audit finding M-6)

- **Status:** Accepted
- **Gate:** Stage-4 MEDIUM/LOW sweep — audit finding **M-6** (landing-zone half).
- **Addresses:** `docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md:183` — "Unbounded
  storage growth: landing-zone orphans + no dead-letter retention." (Dead-letter retention was
  already closed by ADR 0053; this ADR closes the landing-zone orphan + disk-growth-signal half.)
- **Classify (reversibility):** **reversible** — default-off; reversal = delete `runner/gc.py`,
  the three settings, and the three gauges. Revisit trigger: real-volume operation shows orphan
  accumulation (the report-only gauge is how we'll know).

## Context

`run_ingest` does `landing.put(key, …)` BEFORE the windowed `session.commit()`
(`runner/ingest.py`). The landing key is deterministic — `{connector_id}/{dataset}/{record.key}.json`
— so a crash-then-replay **overwrites** the same S3 key and produces no orphan, *provided
`record.key` is deterministic*. A landing-zone **orphan** therefore arises only from a
non-deterministic `record.key` on replay (or a genuinely-crashed mid-window never replayed):
a landed object that no committed DB row references. With no cleanup these accrete until the
landing bucket fills, at which point `landing.put` fails and ingest halts with no signal.

## Decision

1. **Reference-based GC, not a TTL.** Landing bytes are **provenance** — they must persist as
   long as any entity derived from them does, so an age/lifecycle TTL is **wrong** (it would
   delete valid provenance). The GC instead treats an object as an orphan iff its `s3://` URI is
   referenced by NEITHER `ErQueueItem.source_record` NOR `IngestDeadLetter.source_record`. The
   GC's URI string is built identically to `LandingStore.put`'s return (`s3://{bucket}/{key}`),
   so a referenced object can never be misclassified.

2. **Grace window closes the put-before-commit race.** `gc_landing_orphans(..., min_age_seconds)`
   only considers objects whose `LastModified` age exceeds the grace window. A recent
   put-before-commit object is younger than the window and is never swept; an object with no
   `LastModified` metadata is treated as recent (conservative). Default `min_age_seconds=86400`
   (1 day).

3. **Report-only by default; deletion opt-in.** Two gates: `landing_gc_enabled` (master, default
   `False` — the pass never runs in the maintenance cadence) and `landing_gc_delete_enabled`
   (default `False` — when the pass runs it is REPORT-ONLY). The **disk-growth signal** (orphan
   count + orphan bytes + total objects) is ALWAYS computed and exposed via Prometheus, so an
   operator gains visibility before ever enabling deletion.

4. **Fail-loud deletion.** When `delete=True`, candidates are deleted in ≤1000-key batches
   (`LandingStore.delete_keys`); a non-empty `Errors` array raises `RuntimeError` immediately —
   the same discipline as `delete_prefix` (ADR 0049). No silent under-reporting.

5. **Maintenance cadence.** The pass runs inside `Driver.run_maintenance` (ADR 0075 D1), gated by
   the master flag, and caches its `GcStats` on the driver; the on-scrape `DriverMetricsCollector`
   reads the cached stats (no expensive per-scrape bucket list) and emits
   `worldmonitor_landing_objects`, `worldmonitor_landing_orphans`, `worldmonitor_landing_orphan_bytes`.

6. **Deterministic-key invariant (the real prevention).** A test asserts the built-in connectors'
   `record.key` is deterministic for the same input — the property that makes replay overwrite
   rather than orphan. The GC is the backstop, not the primary fix.

## Consequences

- No schema change; no person-affecting / live-ER change; default-off ⇒ no behaviour change until
  an operator opts in.
- The three new gauges become visible to the ADR 0078 Prometheus scrape automatically (the
  alert-rules parity test derives the emitted set from `collector.py` source). No alert rule is
  added here; an operator can add a `worldmonitor_landing_orphan_bytes` alert once they enable the
  pass.

## Out of scope

- Auto-tuning the grace window; per-source GC; an alert rule for the orphan gauges (ops); raw
  MinIO disk metrics (scrape MinIO's own exporter). A connector found to emit a non-deterministic
  `record.key` is reported, not fixed here.

## Tests

- **Integration (testcontainers MinIO):** referenced + old-unreferenced + recent-unreferenced
  objects → `delete=True` deletes ONLY the old-unreferenced one; referenced and recent survive;
  `GcStats` exact. A `delete=False` case deletes nothing yet still reports the orphan.
- **Unit:** reference-set union from both tables; grace-window/age filter; no-`LastModified` →
  recent; fail-loud on a partial `Errors` array; the deterministic-key invariant.
