# ADR 0022 — Connector output: strict FtM validation (fail-loud), not drop-and-log

> Status: **LOCKED** · June 2026 · Implements the L2-is-the-contract rule (CLAUDE.md).

## Context
L2 (the ontology) is the contract: connectors must *produce* valid FtM/STIX entities. A connector that
emits a malformed entity can either be **rejected loudly** (raise) or **silently dropped + logged**.

## Decision
Validate **strictly** at the L2 boundary: `map()` runs `validate_or_raise` on every entity
(`plugins/ftm_bulk.py:23-27`, `ontology/validation.py`), so an invalid entity aborts rather than slipping
through. Connector instance config is likewise validated against its JSON-Schema (`plugins/base.py:112-117`).

## Status
**LOCKED** for the connector→queue boundary.

## Consequences
- ✅ Bad data surfaces immediately at the source instead of corrupting the graph silently.
- ⚠️ A single bad record currently **aborts the whole ingest run** with no dead-letter queue
  (audit gap **G8**) — acceptable for bulk files, unacceptable for continuous streams. Phase 2 needs a
  per-record quarantine/dead-letter path that preserves fail-loud semantics without losing the batch.
- ⚠️ The strict boundary is **not yet applied to enricher output** (`resolution/pipeline.py:80` writes
  `enrich(...)` results unvalidated, audit gap **G10**) — extend the same `validate_or_raise` before
  external enrichers land.
