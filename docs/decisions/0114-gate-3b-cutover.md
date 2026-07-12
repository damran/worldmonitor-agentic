# 0114 — Gate 3b: the F1 projector cutover (retire the direct write; the log becomes the live SoR)

- **Status:** PROPOSED (2026-07-12) — the 3b-planning-proper decision record. Authored as the plan for the
  human-gated, irreversible cutover; the cutover PR (Gate 3b proper) flips it ACCEPTED with the cosign.
  The detailed working artifact is `docs/fable-review/82_GATE_3B_CUTOVER_PLAN.md`.
- **Date:** 2026-07-12
- **human_fork:** true — Gate 3b is the irreversible consumer of the whole F1 inversion: the direct writer
  is gone, the Postgres log becomes the sole system of record, and `projector.py::project` becomes the sole
  live-graph writer. Data-shape / SoR lock-in on a person-affecting, GDPR-bearing surface ⇒ a genuine human
  fork. The go/no-go and the swap-vs-in-place mechanic (D-1) are the human's to make; this ADR recommends,
  the human gate decides, the cutover PR records the outcome.
- **person_affecting:** true — a `full_rebuild` materialises the whole entity graph (persons included) from
  the log; the surface interacts with the catastrophic-merge sensitivity guard (topic labels) and with
  GDPR erasure (resurrection). **human_cosign REQUIRED before the cutover build** (a genuine `true`, not a
  waiver).
- **human_cosign:** PENDING — required before the cutover code PR (like ADR 0107/0108). This PROPOSED
  planning draft carries no cosign; committing it is the 0106/0107/0108 precedent (a planner-staged
  decision-space document).
- **Realises:** ADR 0095 build steps 4–5 (cut the graph writer over to the projector; retire the direct
  write) and 81 §7 items 8/10/11/12/13/14. **Builds on:** ADR 0099/0100/0101/0102 (spine, fold engine,
  incremental correctness, rebuild-diff guard), ADR 0106/0108 (context-claim + sign-off capture), ADR 0107
  (erasure reaches the SoR), ADR 0110/0111/0112 (WPI), ADR 0113 (Gate 2b backfill — the substrate the
  first sanctioned `full_rebuild` consumes). **Supersedes:** completes the transition ADR 0095 opened
  (Neo4j stops being the live SoR).

## Context

The statement/decision/context-claim log + fold engine are built and dormant (`resolution/projector.py`,
ADR 0100 D4). Neo4j is still the *live* SoR. Gate 3b performs the inversion ADR 0095 decided. It is the
**last, irreversible** gate: once the direct writer (`pipeline.py:540`, `signoff.py:304`, `signoff.py:387`)
is retired, anything the log cannot reproduce is gone from a rebuild and anything it cannot forget is a
permanent GDPR liability. 3b-planning-proper (this ADR + doc 82) makes the unverified surface at cutover
minimal and named, and makes the reconciliation that licenses the step sound.

The plan was **adversarially red-teamed** (three independent skeptics, reproduced against `9494335`). Two
**person-affecting CRITICALs** the first-pass audit missed were found, verified first-hand, and folded into
the plan (see §Decision D-3/D-4). This is recorded so the gaps were *closed by design*, not discovered at
the irreversible moment.

## Decision (the plan; internals in doc 82)

**D-1 — Cutover strategy: SWAP (recommended), not in-place.** First-populate the live `"neo4j"` checkpoint
by folding into a fresh, wiped, fenced target (as the diff-guard already does, `driver.py:417-419`),
reconcile it against live (D-3), then promote it (URI cutover / connection-drain). The projector is
MERGE-only and never deletes, so an **in-place** rebuild orphans every node written under a now-superseded
alias id and freezes CREATE-once edge props. The alias-on-read redirect that would mask this
(`writer.py::resolve_node_id`/`get_entity_by_alias`) is **DEAD CODE** (zero production callers; the real
read surface `graph/queries.py` issues raw `MATCH (n:Entity {id})`), so in-place would serve **stale reads
and double-counts** through every API/MCP route. Swap makes that class of defect impossible and moots §7-8
(no orphans). Reversal cost: swap needs URI-cutover tooling that does not exist yet (a small Gate-3b build
item); revisit trigger: if that tooling is disproportionate, fall back to in-place **plus** wiring
`resolve_node_id` into `queries.py` **or** a one-shot orphan GC. **Human-gate decision.**

**D-2 — Exclusion-surface audit (81 §7-11): the recurring guard's blind spots are acceptable for the
recurring guard, NOT for the irreversible gate.** Doc 82 §3 enumerates every excluded/uncompared axis by
instrument. The operator MUST additionally, once, over the real corpus: verify **anchor parity** (the top
real-data-loss risk — safe only if Gate P1 capture landed AND the Gate 2b backfill RAN), **per-prop
`prov_witnesses` subset**, **topic-label parity** (D-3), **schema-label loss** (`live ⊆ fold`), a **no-id-
materialised-twice** count, and an **id-less-node** count. `datasets` stays excluded as the ADR 0113 D4 E4
semantic redefinition (recorded here + a wire changelog note).

**D-3 — CRITICAL, topic labels (verified).** FtM topics (`sanction`, `role.pep`, `crime`, … 73 codes) are
stored in Neo4j **only as node labels** (`registry.topic ∈ ENTITY_IGNORE_PROP_TYPES`; `generate_topic_labels`),
and the divergence measure **never compares labels**. So topic classifications are invisible to both the
recurring guard AND the one-time reconciliation — while the catastrophic-merge **sensitivity guard reads
them from the live graph** (`sensitivity.py::_risk_within_khop`) and `gds.py` reads them. Steady-state loss
≈ 0 (topics ARE captured as statement rows), but un-logged paths exist (pre-ADR-0113 legacy nodes;
`backup.py::_import_neo4j` DR-restored labels with no statement rows). **A live↔fold topic-label parity
check (both directions, split topic vs schema) is a HARD cutover precondition.** The audit's originally
proposed `fold ⊆ live` direction was the WRONG one (it passes exactly on loss); the loss direction is
`live ⊆ fold`.

**D-4 — CRITICAL, erased-value resurrection on a co-present multi-source node (verified).** `erase_source_graph`
value-prunes a multi-source survivor **in place** (`ops.py:152`, keeps the node) while `scrub_log_lanes`
DELETEs are **staged** for a later commit (`erasure.py:210-211`; non-atomic). A `full_rebuild` in that
window (a crashed/aborted erasure that never converged, or the cutover fold itself) re-folds the un-scrubbed
rows and **re-adds the erased values** to a node that exists on both sides — so it passes `live ⊆ fold`
(R4) and is not a fold-extra (R7/R9). The reconciliation (D-5) therefore adds a **co-present erased-value
check** (R9b) and an **erasure-convergence precondition** (R1b: `scrub_stock` to convergence +
rebuild-contains-no-erased-source before the authoritative fold). Latent today (no live erasure caller),
activated by Gate 3b + any future live eraser.

**D-5 — One-time reconciliation (81 §7-12): the two-directional + count instrument that licenses the step.**
A read-only dress rehearsal (folds into the fenced isolated target, never live), executable via a NEW pure
`resolution/reconciliation.py` (`@given`-tested, Neo4j-free). Beyond the recurring `measure_divergence`
(R4, `live ⊆ fold`), it adds: fold-side-extra enumeration (R7/R8) + erased-source block (R9); the **co-present
erased-value block (R9b)** and **fold→live co-present value check + ledger-correctness spot-check (R9c** — a
wrong-but-logged catastrophic merge is otherwise faithfully replayed, both sides normalised through
`survivor_of`); **count reconciliation with a per-id multiplicity term (R11/R11b** — else a same-id duplicate
carrying an un-logged anchor is absorbed into `alias_collapse` and vanishes count-clean). PASS = all
MUST-gates green + zero resurrection + zero unexplained fold→live value + count/multiplicity residual 0 +
label parity clean.

**D-6 — Retirement carve-outs (81 §7-14) + fail-closed writer guard.** The write census is provably
exhaustive (12 sites; doc 82 §5). RETIRE the three direct writers; the projector becomes the sole live
entity writer; SURVIVE the erasure live-prune (P2 mechanism (a)), the diff-isolated writes, and one-time
DDL. Add `backup.py::_import_neo4j` (a raw-Cypher DR restore that bypasses the log — **OPEN**, recommend
retire in favour of "restore Postgres → `project(full_rebuild=True)`", or keep with a mandatory
post-restore reconcile-to-log). Enforce with a structural CI gate: (a) an AST allowlist of live-write
call-sites; (b) `write_entities` called ONLY from `projector.py::project`; (c) the bolt driver constructed
only in `neo4j_client.py`; **(d) forbid `.driver` attribute access outside `neo4j_client.py`** (the public
`Neo4jClient.driver` field bypasses (a)+(c)). `graph/gds.py::degree_centrality` is dead code → delete at
cutover to shrink the surface. **§7-8** superseded-node deletion is **moot under swap** (no orphans);
under in-place it needs the `queries.py` redirect or an orphan GC (the projector delete step stays reserved
for P2 mechanism (b)).

**D-7 — Driver LOWs (81 §7-13).** BUILD NOW (dormant guard, reversible): **LOW-1** single ledger read
(one `survivor_of` closure threaded through the fold + measure; today the ledger is read 2–3× across
separate sessions) and **LOW-2** handshake-refusal observability (a Prometheus counter so a mis-aliased
diff target is visible, not just logged). **LOW-3** snapshot streaming is **SPEC + DEFER**: the naive lazy
`session.run()` fix is WRONG for the LIVE read (holds the txn open → observes concurrent commits under
READ-COMMITTED → timing-dependent gauge / false green); correct scoping keeps the LIVE read EAGER and
streams only the isolated fold target, with an id-multiplicity-aware (sorted-multiset, not id-keyed-dict)
equivalence test — a focused follow-up gate.

## Open decisions for the human gate (surface at the cutover, not now)

1. **D-1 swap vs in-place** — recommend swap; needs URI-cutover tooling.
2. **`backup.py::_import_neo4j`** — retire vs keep-with-reconcile (D-6).
3. **Routine post-cutover `full_rebuild` concurrency** — quiesced vs concurrent (if concurrent, extend the
   WPI-3 spine-writer lock to cover `project()`, which today skips it and runs unshared READ-COMMITTED reads).
4. **`ensure_constraints` boot hook** — establish anchor-uniqueness before the first authoritative fold
   (today only a DR-restore side effect).
5. **Promote `prov_witnesses` / node `datasets`** from convenience/derived exclusions into compared axes,
   or defer with the recorded revisit trigger.
6. **GDPR orphan-reachability** rests on the provenance-match erase leg only (`ops.py:88-92`), not the
   survivor-keyed prune leg — qualify or add a backstop.

## Reversibility

3b-planning-proper is fully reversible (docs, this PROPOSED draft, additive Neo4j-free reconciliation
instruments, LOW-1/LOW-2 on a default-off guard). The **cutover** is the irreversible step and is NOT
executed here; it awaits the operator preconditions (doc 82 §8: run the 2b backfill, erasure convergence,
guard green over N cycles, the reconciliation PASS) and the human sign-off. Overall F1 revisit trigger is
unchanged from ADR 0095 (fold-maintenance cost exceeding the pain it removes; the rebuild-and-diff job is
the early-warning signal).

## ADR-index coupling

Filed as `docs/decisions/0114-gate-3b-cutover.md`, H1 `# 0114 — …`, machine-checkable fields (`Status`,
`Date`, `human_fork`, `person_affecting`) plain/un-bolded value tokens in the first ≤15 lines; the
`human_fork` line carries only the `true` token (no opposite literal, so it does not parse `mixed`).
`human_cosign` is not index-parsed. Authored **PROPOSED**; per the house pattern the **cutover PR after the
cosign** owns the accept flip (stamp the dated `human_cosign` line, PROPOSED → ACCEPTED,
`python scripts/gen_adr_index.py` regen). The accept flip must not occur on a PENDING cosign line.
