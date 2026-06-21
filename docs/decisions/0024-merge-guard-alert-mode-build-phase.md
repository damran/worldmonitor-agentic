# ADR 0024 — Catastrophic-merge guard: alert-only mode for the build phase

> Status: **LOCKED (build phase, TEMPORARY)** · June 2026 · Supersedes [0020](0020-merge-guard-thresholds.md)
> for the build phase. Format: Context → Decision → Status → Consequences.

## Context
ADR 0020 set the catastrophic-merge guard to **block**: a cluster that is oversized
(`>10` members) or contains a PEP / sanctioned / criminal member is parked in
`pending_review` and **never written** to the graph. That is the correct production
posture (CLAUDE.md: *never auto-merge a sensitive entity; human review for
high-impact merges*).

During the **build phase**, however, there is no human review queue UI and no
operator watching it. Parking every flagged cluster stalls the very pipeline we are
trying to exercise end-to-end: a single sanctioned entity in a bulk import silently
removes its whole cluster from the resolved graph, so the graph under construction is
incomplete and the rest of the spine can't be tested against realistic data. Per an
explicit user decision for the build phase, the guard should **alert, not block** —
let flagged merges proceed, but leave a durable, auditable trail.

This is **not** a relaxation of the guard's *evaluation*. `is_sensitive` /
`needs_review` (`resolution/review.py`) and the `MAX_AUTO_MERGE_SIZE = 10` threshold
are unchanged; only the *action* taken on a flagged cluster changes, and only because
a config flag says so.

## Decision
Add `MERGE_GUARD_MODE` (`settings.py`, env `MERGE_GUARD_MODE`), a flag — not a code
deletion — that selects the **action** on a flagged cluster. The guard evaluation
branches on nothing; only the pipeline's response does (`resolution/pipeline.py`):

- **`block`** — unchanged ADR 0020 behavior: record `pending_review` in
  `merge_audit`, set the queue items to `pending_review`, and never write the merge.
- **`alert`** (build-phase **default**) — **write the merge** (promote it like any
  other) **and** record a durable row in the new `merge_alerts` table
  (`db/models.py`) capturing `tenant_id`, the canonical id, the collapsed source
  entity ids, the sensitivity reason (oversized / PEP / sanctioned / topic), the match
  score, and a timestamp. A `WARNING` is logged with a running per-run count.

`merge_alerts` is the audit trail an operator reviews when flipping the flag back to
`block` after the build is complete. Thresholds and the sensitive-topic vocabulary
are untouched (still ADR 0020 / gaps G5–G6).

## Status
**LOCKED for the build phase — explicitly TEMPORARY.** Before production the system
**MUST** return to `MERGE_GUARD_MODE="block"` with **human sign-off** (CLAUDE.md
self-improvement rule: changes affecting a real person always need human sign-off),
and the accumulated `merge_alerts` rows **MUST** be reviewed at that point. This ADR
supersedes 0020 *only* for the build phase; 0020's blocking decision is the production
target and is restored by the flip.

## Consequences
- ✅ The full ingest → resolve → graph spine runs end-to-end during the build without
  a human-review UI; flagged merges no longer silently disappear from the graph.
- ✅ Nothing is lost: every flagged-but-merged cluster is durably recorded in
  `merge_alerts` (reason + collapsed ids + score), reviewable later. Flipping the flag
  to `block` restores ADR 0020 exactly, with no code change.
- ⚠️ **While in `alert` mode, sensitive / oversized merges proceed UNREVIEWED and may
  fuse distinct real entities** (e.g. two different sanctioned people with the same
  name). `merge_alerts` is the audit trail, not a safeguard against the bad merge
  itself. This is acceptable *only* because the build-phase graph is not yet a
  production decision surface.
- ⚠️ The guard's protective guarantee from ADR 0020 (sanctioned merges provably held)
  does **not** hold while in `alert` mode. It is restored only on the flip back to
  `block` with sign-off and a review of accumulated alerts.
