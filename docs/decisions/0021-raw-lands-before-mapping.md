# ADR 0021 — Raw record lands in object storage before mapping/enqueue

> Status: **LOCKED** · June 2026 · Implements the provenance / replayability invariants.

## Context
Connectors `collect()` raw bytes and `map()` them to FtM entities. The provenance pointer on every entity
must reference a **real** raw record, and we want to be able to re-map raw data later without re-fetching
from the source (replayability, audit).

## Decision
In `run_ingest` (`runner/ingest.py:48-76`), each collected `RawRecord` is **written to the landing zone
first** (`landing.put` → returns the URI), and only then is the entity mapped and enqueued with
`source_record = uri`. Raw lands verbatim under a `tenant/connector/dataset/key` path
(`runner/ingest.py:53`); the bucket is ensured up front (`ensure_bucket`).

## Status
**LOCKED.**

## Consequences
- ✅ The s3:// provenance pointer is concrete before any candidate exists; entities are always traceable
  to stored raw bytes.
- ✅ Raw can be re-mapped if a mapper improves, without re-hitting the source (respects passive/active
  + rate limits).
- ⚠️ `ensure_bucket` currently swallows all `ClientError` as "exists" (audit gap **G11**) — a permission/
  network failure is hidden until a later `put()`. Tighten before Phase 2 operations.
- ⚠️ No lifecycle/retention policy on the landing zone yet; raw grows unbounded.
