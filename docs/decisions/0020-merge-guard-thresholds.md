# ADR 0020 — Catastrophic-merge guard: hardcoded conservative thresholds (v0)

> Status: **LOCKED** (v0) · **superseded by [0024](0024-merge-guard-alert-mode-build-phase.md)
> for the build phase** · June 2026 · Implements the catastrophic-merge-guard invariant (CLAUDE.md).
>
> **Note:** the guard *thresholds* and *evaluation* below remain in force. ADR 0024 changes only the
> build-phase *action* on a flagged cluster (alert-and-write instead of park-for-review) behind the
> `MERGE_GUARD_MODE` flag; this blocking behavior is the production target, restored by flipping the
> flag back to `block` with human sign-off.

## Context
The catastrophic-merge guard (PR #12) must hold high-impact merges for human review and never auto-merge
a sensitive entity. v0 has no rule engine or config surface to express these policies.

## Decision
Encode the guard as **hardcoded conservative defaults** (`resolution/review.py:16-50`):
- A cluster collapsing **more than `MAX_AUTO_MERGE_SIZE = 10`** source entities is routed to
  `pending_review`, never auto-promoted.
- A cluster containing **any PEP / sanctioned / criminal member** is routed to review. Sensitivity is
  decided by `is_sensitive`, which matches a frozen `SENSITIVE_TOPICS` set plus `role.pep*` / `sanction*`
  topic prefixes (`review.py:19-31`).
- Flagged clusters are recorded with their reason and **never written to the graph**
  (`resolution/pipeline.py:69-85`); proven by `test_resolve_pending_pipeline`.

A configurable rule-engine surface for these thresholds is **deferred**.

## Status
**LOCKED** for v0. Moving thresholds and the sensitive-topic vocabulary to config / a rule plugin is a
later decision (Phase 4+, when enrichers add domains).

## Consequences
- ✅ Conservative-by-default: errs toward human review; sanctioned merges are provably held.
- ⚠️ `MAX_AUTO_MERGE_SIZE = 10` is arbitrary and **untested** at the boundary (audit gap **G5**).
- ⚠️ `SENSITIVE_TOPICS` is **OpenSanctions-specific** and depends on a `topics` property; a future
  enricher with a different vocabulary (CTI, crypto) would bypass the guard (audit gap **G6**). Extend to
  a registry/config before Phase 4 enrichers.
- ⚠️ Tuning these thresholds affects real people (ER outcomes), so per the self-improvement rule any
  automated change needs human sign-off.
