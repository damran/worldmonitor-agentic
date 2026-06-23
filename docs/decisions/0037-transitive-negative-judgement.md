# 0037 — Transitive enforcement of negative (reject) judgements (audit H-1)

- **Status:** accepted (implemented 2026-06-23)
- **Date:** 2026-06-23
- **Addresses:** `docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md` finding **H-1**
- **Touches:** `resolution/merge.py` (`cluster_and_merge` — judgement *consumption*). Builds on [0031](0031-return-to-block-signoff.md) (durable sign-off judgements), [0028](0028-per-batch-resolver-isolation.md) (ephemeral resolver), [0036](0036-deterministic-canonical-id.md) (B-1; its Part 2 deliberately left judgement *consumption* untouched so this fix is clean).

## Context

Audit finding **H-1**: a human REJECT held only against a *direct* re-merge. `cluster_and_merge`
seeded the durable judgements, then skipped only the **exact** decided pair before applying Splink
positives (`if key in decided_pairs: continue`). So a later batch with a **bridging record** — B with
Splink `A~B ≥ 0.92` and `B~C ≥ 0.92` — applied both positives and **transitively re-fused** the
rejected pair A–C into one canonical node. The graph then asserts **A ≡ C**, silently overriding the
human "these are distinct". This is the same class of corruption as B-1 (a false identity in the
product graph), reached by a different route, and it defeats the merge→park→reject→no-re-park
guarantee validated on real data (which only covered direct re-merge). Reproduced in the audit and in
`tests/unit/test_resolution_negative_judgement.py`.

## Decision

**Strong reading — a negative judgement is a cannot-link: A and C may never co-cluster, directly or
transitively.** (The weak reading — "forbid only the direct A–C edge" — does **not** fix the corruption:
the transitive chain still fuses A and C, so the graph still asserts A ≡ C and the human is still
overridden via B. Honoring the reject *in the graph* requires the strong reading; it was never a real
alternative.)

**Mechanism — pre-merge consultation (not post-cluster validation).** In `cluster_and_merge`:
1. Apply Splink positives **strongest-first** (`sorted(pairs, key=probability, reverse=True)`).
2. Before applying each positive, consult **`resolver.get_judgement(left, right)`** and **skip** the
   pair if it returns `NEGATIVE`. nomenklatura's `get_judgement` is **component-aware**
   (`resolver/resolver.py:325-330`): it reports `NEGATIVE` when a negative edge exists between *any*
   members of the two ids' positive-connected components — so once A and B are connected, the bridging
   `B~C` positive is flagged by the seeded A–C negative and suppressed. The library's own semantics do
   the transitive reasoning; we do not hand-roll constrained clustering.
3. **Log** each suppression at WARNING so the enforcement is observable.

**Auto-resolve by score, not re-park.** Only the reject-crossing link is dropped; the other (valid)
links still merge, so the bridging record B joins its **highest-confidence** side (deterministic via the
strongest-first ordering). We do **not** re-park the whole cluster for review — that would block the
unrelated, valid A~B merge on review noise. Any *sensitive* merge among the surviving links is still
parked by the catastrophic-merge guard as usual.

### Why pre-merge over post-cluster
- Never materializes the corrupt {A,B,C} cluster — no teardown / split-ambiguity logic.
- Uses nomenklatura's native component-aware negatives (the intended `check_candidate`-before-merge
  usage) rather than reimplementing cannot-link/must-link conflict resolution.
- Deterministic and explainable: "B merged with its strongest match; the link crossing the reject was
  suppressed." Post-cluster splitting would have to re-derive the same placement anyway.

### Reversibility (what makes the strong reading safe, not permanent)
The reject holds only while the negative judgement exists. A later **approve** (a positive judgement on
A–C) reverses it: `get_judgement` reports `POSITIVE` for a positive-connected pair **before** it consults
negatives (`resolver/resolver.py:312`), so an approved pair co-clusters cleanly. A human can always
revisit. (Proven by `test_reject_is_reversible_by_a_later_approve`.)

## Consequences

- ✅ A human reject is honored **transitively** — no bridging record can silently re-fuse a rejected
  pair. Closes H-1.
- ✅ The valid links still merge; the bridging record lands on its strongest side; the suppression is
  logged. Reversible by a later approve.
- ➖ A single reject can break an A~B~C chain where both Splink links look strong. This is **correct**:
  identity is transitive, so A=B ∧ B=C ∧ A≠C cannot all hold — at least one Splink link is wrong (or the
  human is), and sign-off exists to trust the human. The break is resolved in the human's favor.
- ⚠️ **Observability is via a structured WARNING log**, not a durable DB row — consistent with the
  existing alert-mode / schema-incompatible-skip logging, and chosen to keep this fix scoped to
  `merge.py` (`cluster_and_merge` stays a pure, DB-free, unit-testable function) and **migration-free**.
  A durable, queryable suppression audit (e.g. the pipeline writing a `merge_audit` row with
  `decision="suppressed"` from data returned by `cluster_and_merge`) is a clean follow-up if history is
  wanted; not built here.
- ⚠️ `get_judgement` is `O(|comp(A)| × |comp(C)|)` per pair; negligible for the bounded within-batch
  clusters of v0. Worth noting for the later ER-performance work (audit B-3 / L-5), not a concern now.

## Verification

`tests/unit/test_resolution_negative_judgement.py` (pure unit tests on `cluster_and_merge`, no Splink/DB):
- **transitive suppression** — reject A–C + bridging B → A and C not co-clustered (**fails pre-fix**);
- **by-score placement** — B joins its higher-scored side (**fails pre-fix**);
- **direct-reject regression** — a rejected direct pair never merges (Legion-style; passes pre & post);
- **reversibility** — flipping the judgement to approve co-clusters A,B,C (**fails pre-fix on the reject half**);
- **no false suppression** — a clean chain with no negative still merges fully (passes pre & post);
- **observability** — the suppression emits the WARNING log (**fails pre-fix**).
Confirmed: the four new-behavior tests fail on pre-fix `merge.py`; the two guard tests pass both ways.

## Relationship to B-1 / scope

B-1 Part 2 (ADR 0036) kept judgement **write** semantics unchanged precisely so this **consumption**-side
fix would be clean; the two compose without overlap. This ADR fixes **H-1 only** — no B-2/B-3, no Gate
B/C/S4, no G3.
