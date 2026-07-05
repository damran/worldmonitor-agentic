# 81 — Pre-cutover gate sequence: converting the log-capture consult into prerequisite gates

- **Status:** PLANNING (2026-07-05) — sequences the binding prerequisites the Fable log-capture consult
  (`80_LOG_CAPTURE_CONSULT.md`) named, ahead of the human-gated, LAST, writer-cutover (Gate 3b proper).
  This document decides **sequencing + ownership**; each gate's own ADR decides its internals.
- **Primary input:** `docs/fable-review/80_LOG_CAPTURE_CONSULT.md` (§4 capture, §5 E4, §6 forgetting,
  §6b sign-off, §7 the 14-item checklist, §8 repo defects). **Frame:** this is pure data-lineage /
  storage-governance engineering on an append-only-log → derived-projection substrate —
  capture-completeness, right-to-forget, and human-decision durability. Kept in those primitives.

## The target (unchanged from the consult §1)

Cutover (Gate 3b) makes **rebuild-from-the-log the routine DR/verification path** and retires the direct
Neo4j write. That is **irreversible** in the sense that matters: once the direct writer is gone, anything
the log cannot reproduce is gone from a rebuild, and anything the log cannot forget is a GDPR liability
forever. The consult's four narrower properties (not "100% reconstructable") are the gate conditions:

1. **Rebuild destroys nothing load-bearing** → capture E2 (**P1**); log the sign-off lane (**P3**).
2. **Rebuild resurrects nothing erased** → erasure scrubs all three log lanes, flow + stock (**P2**).
3. **Forgetting reaches the live graph by a defined mechanism** → a live-removal mechanic survives
   write-path retirement (**P2**).
4. **The unverified surface at cutover is minimal and named** → the exclusion-surface audit + the
   one-time reconciliation (**3b-planning-proper**).

## Sequence (and why this order)

```
P1  context-claim capture lane            (person_affecting:false, cosign — additive)   ← BUILD FIRST
        └─ Slice P1-0 docs errata (independent, no cosign, ships during the P1 cosign pause)
P3  sign-off spine durability             (person_affecting:TRUE, cosign)
P2  erasure reaches the SoR               (person_affecting:TRUE, cosign — the largest gate)
WPI write-path-integrity slices           (small, mostly independent; interleave from P1 onward)
2b  backfill + E4 rider + dataset-stamp   (its own gate; pre-req for guard-green + reconciliation)
3b-planning-proper                        (exclusion audit, reconciliation, driver LOWs, carve-outs)
3b  cutover + retire the direct write     (human-gated, LAST)
```

**Why P1 first.** It is the only additive, non-person-affecting gate of the three, and it is on the
**lock-in-critical edge**: once an enricher is wired, un-captured enricher output is
unreconstructable-forever. P1 lands the capture writer + interface before any enricher; it also un-sticks
the divergence guard (which today false-alarms on every anchored node against a fold that cannot produce
anchors — consult §2/§8 defect 1), making the pre-cutover verification instrument usable at all.

**Why P3 before P2 — capture-before-forget.** Three reasons: (i) P3 is the consult's discovered
**CRITICAL** — `signoff.approve()/reject()` writes the live graph with **zero** spine rows, so a rebuild
silently drops **every human-approved merge**, the most protected content in the graph. (ii) P3 unblocks
the divergence guard on any corpus where a park+approve ever occurred (§7-10 deadlocks otherwise). (iii)
**P2's log-scrub completeness layers on P3**: you cannot scrub-from-the-log what was never logged. Until
P3 routes sign-off through the spine, sign-off-approved nodes are live-only; making the *reconstructable*
log forgettable (P2) is the clean second step only once everything that must be reconstructable is
actually in the log (P1 anchors + P3 sign-off). The consult's principle §3 states it directly:
"reconstructability must be bounded by the right-to-forget" — you bound what exists, so establish
existence (P1, P3) before bounding it (P2). P2 is also the largest, forkiest gate (three-lane scrub,
live-removal mechanism fork, granularity reconciliation, stock scrub, P-FOLD-2 deletion bound) — best
built on the fullest captured substrate.

---

## Gate P1 — evidence-capture lane (BLOCKING; ADR 0106; spec `GATE_P1_CONTEXT_CLAIM_CAPTURE_SPEC.md`)

Realises consult §7-1 + §8 defects 1/2. **Scope:** a second append-only `context_claim` lane in the SoR
spine (`canonical_id · entity_id · key · value · dataset · method · retrieved_at · seq · scope ·
created_at`; `method` + `retrieved_at` NOT NULL); INSERT-only writers in `resolution/statements.py`
written at **both** live promote points (pipeline promote block + `signoff.approve()`); the fold
reproduces anchors (full + incremental with a `last_context_claim_seq` watermark + touched-survivor full
context-history re-read + the `_assign_sqlite_seq` SQLite listener extended to the new `seq`); the dead
`divergence.py:85` `wm_anchor_` branch deleted → bare `CANONICAL_ID_FIELDS` exclusion (pick-semantics);
anchored corpora added to IT-PROJ / P-FOLD with anchor-parity assertions. `person_affecting: false`
(additive) but the `resolution/**` diff ⇒ ADR-0097 cosign PENDING. Every P1 sub-fork is classified by
reversibility in ADR 0106; all are reversible except the correctness-required incremental touched-set.
**Zero-prop disposition (§7-3) is DEFERRED out of P1** (Sub-fork H; a different evidence class, and
folding it in would expand the blast radius into the promote *decision*).

## Gate P3 — sign-off spine durability (ADR 0108 draft; person-affecting, cosign)

Realises consult §7-2 + §6b (the discovered CRITICAL). **Scope:** `approve()` co-commits the same spine
writes the pipeline promote block does — statement rows for the fused canonical (+ the P1 context lane,
already wired), a **decision row with `decided_by=<operator>`** (the human-decision path ADR 0099
explicitly reserved), and whatever ledger write (`record_durable_id`) makes the survivor resolvable
(sign-off merges are currently invisible to `survivor_of` — no alias row). `reject()` gets the
member-write equivalent. **Design fork to decide in P3's ADR:** *co-commit the spine writers at
`approve()`* vs *re-route `approve()` through the pipeline promote point* — recommend the **co-commit**
(smaller blast radius; `approve()` already builds the merged canonical + members; the pipeline promote
point assumes a Splink-clustered `ResolvedCluster` that sign-off does not have). **Ordering note:**
`approve()` writes the graph **before** the Postgres commit (opposite of the pipeline's write-after
ordering) — the spine writes must land in the same transaction as the existing `SignOff`/judgement rows
so a crash rolls them back together; the existing B-1 idempotent-re-run recovery (graph MERGE + audit
mutate) is preserved. Person-affecting (it alters the human sign-off mechanics) → cosign + mandatory
`@given`.

## Gate P2 — right-to-forget reaches the SoR (ADR 0107 draft; person-affecting, cosign; the largest gate)

Realises consult §7-4/5 + §6. **Scope — both directions:**
- **Non-resurrection (log side):** a value-level scrub across **all three lanes** — `statement`
  (`DELETE … WHERE`), `context_claim` (same), and `decision` rows referencing erased members
  (likely **tombstone/redact** rather than delete, since decision rows are judgements — the gate's ADR
  decides). An erasure gate scoped to statements alone would resurrect erased-source anchor claims on
  rebuild.
- **Live removal (graph side):** reprojection **cannot** enforce removal (the projector only MERGEs +
  additively `SET`s; a `DELETE` emits no `seq` row so the incremental fold never revisits the scrubbed
  survivor). Pick a **defined mechanism** (consult §6 a/b/c). **Recommended reversible DEFAULT: (a) keep
  `graph/ops.py`'s direct prune as a permanent, explicitly-carved-out second live-writer** that survives
  write-path retirement (§7-14). **Reversal cost:** low (the prune already exists; the carve-out is a
  documented exception, not new machinery). **Revisit trigger:** if the direct prune becomes hard to
  keep correct against a MERGE-only projector, switch to **(b) seq-bearing erasure-event rows the
  projector consumes with delete capability** (which would also give superseded-node deletion a home —
  see §7-8). Do **not** manufacture a human fork on the mechanism choice — but the gate as a whole is
  person-affecting → user cosign.
- **Granularity reconciliation:** the live prune is prop-granular (a co-witnessed prop keeps all values);
  the log scrub is row-granular. Unreconciled, every real erasure leaves live value-sets exceeding the
  fold on compared props → permanent unexplained divergence (§7-10 deadlock). The gate aligns them.
- **Stock, not just flow:** a one-off retroactive scrub of every erasure executed during the dual-write
  window (driven from the erase-audit records; `DELETE FROM statement/context_claim WHERE dataset =
  <erased source_id>`), verified by a rebuild-contains-no-erased-source check.
- **The round-trip property asserts BOTH surfaces:** erase → (i) `full_rebuild` into a fresh target
  contains nothing of the erased source, AND (ii) the live graph no longer holds the erased values. A
  fresh-target-only oracle goes green while the live graph still holds everything.
- **Fold-suite impact:** P-FOLD-2 (incremental == full) is proven under a no-deletion bound; P2 bounds or
  extends it for deletions (a scrub between batches breaks naive incremental-vs-full unless the erased
  survivor is re-folded / event-driven).

## Remaining §7 items — owner map

| §7 item | Owner | Note |
|---|---|---|
| 1 E2 capture live | **P1** | ADR 0106 |
| 2 sign-off routed through the spine | **P3** | discovered CRITICAL |
| 3 zero-property promoted entities | **WPI-1** (write-path-integrity slice, post-P1) | different evidence class; keep out of P1's blast radius (ADR 0106 Sub-fork H) — capture an existence claim, reject at promote, or a documented exclusion; decided + tested |
| 4 erasure round-trip BOTH surfaces | **P2** | three-lane scrub, granularities reconciled, P-FOLD-2 bounded |
| 5 retroactive stock scrub | **P2** | one-off, verified by rebuild-contains-no-erased-source |
| 6 alias⇔co-commit invariant | **WPI-2** (small, independent; sequence near P3) | 3a-ii-A HIGH backlog; independent of item 2 (sign-off writes no alias, so both are needed) — any producer writing a ledger alias co-commits the survivor's statements, or the fold gains a completeness check |
| 7 single-writer ingest assert | **WPI-3** (small, independent; can land early/parallel) | ADR 0100 D1's stated assumption; assert/enforce, or build the min-in-flight-seq watermark; post-cutover a violation is permanent live loss |
| 8 superseded-node deletion owner | **3b-planning-proper** (P2-rider IF P2 picks mechanism §6-b) | ADR 0102 defers it OUT; 3b is the last named owner; a projector/guard delete step, or documented alias-on-read staleness + revisit trigger |
| 9 Gate 2b backfill landed | **Gate 2b** | pre-2a nodes into the log; also resolves `statement.dataset` stamped-ness + the E4 §5 `origin_datasets` rider; until then the guard honestly reports pre-log nodes as unexplained |
| 10 guard enabled + green over N cycles | **operational** (3b-planning-proper checklist) | explicitly depends on items 1, 2, 4, 9 (anchored/sign-off/erased corpora cannot go green before them) |
| 11 exclusion-surface audit (split by instrument) | **3b-planning-proper** | P1 pre-populates the `wm_anchor`/bare-key exclusion (pick-semantics); divergence predicate vs equivalence signature are different surfaces |
| 12 one-time two-directional / count reconciliation | **3b-planning-proper** | at the cutover moment; enumerate every fold-side extra and explain it as E1; the D6 blind spots are acceptable for the recurring guard, not the irreversible gate |
| 13 3a-ii-B LOWs | **3b driver work** (3b-planning-proper) | single ledger read; handshake-refusal observability; **snapshot scale** (stream/batch the whole-graph reader — production-scale guard/DR depends on it) |
| 14 write-path retirement carve-outs | **3b-planning-proper / 3b cutover** | the erasure live-prune (§6 option a) and the diff-guard's isolated-target writes survive; anything else that writes the live graph after cutover is a defect |

## Docs truth-up (assigned to Gate P1, Slice P1-0 — docs-only, its OWN PR, ships FIRST, no cosign)

- **`docs/40_ROADMAP.md`** — the `## Next — Gate 0 … ★ CURRENT` marker (line ~56) is **stale** (Gate 0
  shipped; Phase-3 infra shipped; the fleet is at the pre-cutover P-gate sequence). Slice P1-0 moves the
  `★ CURRENT` marker onto **Gate P1**, pointing at this document.
- **ADR 0100 erratum** — the E2 ordering parenthetical is backwards (`enrich` at `pipeline.py:437`
  precedes `record_statements` at `:483`); D3's "Anchors — not reconstructed (E2)" note is retired by P1.
- **ADR 0102 erratum** — D6's `wm_anchor_*` key-shape text is wrong (nodes carry bare `CANONICAL_ID_FIELDS`
  keys; the exclusion was dead code, fixed in P1).
- **The planning corpus + PROPOSED drafts** — this document, the P1 spec, and ADRs **0106 + 0107 + 0108
  (all PROPOSED; 0107/0108 explicitly awaiting-cosign-before-build)** are committed here, with the
  `gen_adr_index.py` regen (three new rows). Rationale: the index generator scans the filesystem, so
  untracked drafts break `--check` locally and would poison the P1 code PR's regen with rows for
  uncommitted files (adversarial-verify finding, HIGH). Committing PROPOSED drafts is the 0101/0102
  precedent; each gate still owns its own accept flip.

Slice P1-0 ships **before** the P1 code PR (which then re-cuts/rebases onto the merged master) and is
autonomously mergeable — it touches no code and needs no cosign.

## What this document does NOT decide

- Any gate's internals beyond scope + ownership — the P2 decision-row erasure semantics, the P2
  live-removal mechanism final pick, the P3 co-commit-vs-re-route choice, and the anchor conflict
  semantics (settled for P1 in ADR 0106) belong to each gate's own ADR (this doc + the consult as input).
- Cutover mechanics (checkpoint promotion, retire-direct-write sequencing) beyond the checklist
  conditions — that is Gate 3b-planning-proper, which the completed P1/P2/P3 + WPI + 2b work unblocks.
