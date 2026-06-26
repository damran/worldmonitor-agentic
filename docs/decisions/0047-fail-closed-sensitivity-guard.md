# ADR 0047 — Fail-closed sensitivity guard (deny-by-default; topics → graph → Chow abstain)

> Status: **ACCEPTED** · 2026-06-25 (slice-3 refinement 2026-06-26) · Closes audit gap **G6** · Inverts the denylist half of **ADR 0020**
> Gate: [Gate E — Fail-Closed Sensitivity Guard](../reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md)
> Person-affecting: **NO (fail-closed)** — the change can only move MORE clusters to human review;
> it auto-promotes nothing and needs no sign-off (CLAUDE.md self-improvement rule).

## Context

CLAUDE.md is non-negotiable: *"never auto-merge a sensitive entity"* and *"human review for
high-impact merges."* The catastrophic-merge guard (`resolution/review.py`, ADR 0020) decides
sensitivity from a **hardcoded denylist** that **fails OPEN**:

- `SENSITIVE_TOPICS` (`review.py:22`) is **7 codes**; with the `role.pep*`/`sanction*` prefix rule
  (`review.py:33`) it catches **10** of FtM 4.9.2's **28** risk topics (`registry.topic.RISKS`). It
  **MISSES 18** — including `crime.war`, `crime.boss`, `role.rca`, `role.oligarch`, `debarment`,
  `export.control`, `export.control.linked`, `sanction.control` *(caught by the prefix)*, `reg.action`,
  `reg.warn`, `invest.ban`, `crime.fin`, `crime.theft`, `crime.traffick`, `mare.detained`,
  `mare.shadow`, `export.risk`, `invest.risk`, `corp.disqual`. A cluster whose only risk signal is one
  of those auto-merges with **no human review**.
- `is_sensitive` reads `topics` with `quiet=True` (`review.py:30`): an entity with no `topics`
  property (e.g. a `Sanction` whose risk is on its *target*) returns `[]` → **not flagged**.
  Structural risk is invisible.

ADR 0020 itself flagged this (its Consequences §: *"`SENSITIVE_TOPICS` is OpenSanctions-specific …
a future enricher with a different vocabulary would bypass the guard — audit gap **G6**. Extend to a
registry/config before Phase 4 enrichers."*). This ADR is that extension, brought forward because the
hole is a live catastrophic-merge fail-open, not a Phase-4 nicety. ADR 0020's **size** half
(`MAX_AUTO_MERGE_SIZE = 10`, G5) is conservative-by-default and is NOT inverted here.

`needs_review` is **pure** (no Neo4j handle), called once at `pipeline.py:357`; a flagged verdict
flows into the existing ADR-0031 `pending_review` → sign-off sink (`pipeline.py:363-367`). After it,
the **approved-group exemption** (`pipeline.py:359-360`) un-flags a cluster whose members are a subset
of a human-approved positive-judgement group.

## Decision

Invert the guard's sensitivity axis from **allow-unless-denylisted** to **hold-unless-provably-benign
(deny-by-default)**, evaluated in three ordered stages in a new `src/worldmonitor/guard/sensitivity.py`
into which `resolution/review.py` (`needs_review`, `is_sensitive`) **delegates**.

### 1. Programmatic risk source — FtM's own risk tag, not our list

The sensitive topic set is loaded at runtime from `from followthemoney.types import registry;
registry.topic.RISKS` — a `set[str]` of 28 codes (FtM 4.9.2, `followthemoney/types/topic.py`),
**FtM's own risk classification**, never copied into code/config as a literal. The legacy
`SENSITIVE_TOPICS` constant and the `role.pep*`/`sanction*` prefix rule are **deleted** (every code
they matched is in `RISKS`). Verified verbatim in `VERIFIED_API.md` before code (gate spec §2).

### 2. Unknown ⇒ sensitive (the inversion hinge)

A topic code **not in `registry.topic.names`** at all (off-ontology — an enricher / CTI / crypto
vocabulary the FtM model has never seen) is treated as **sensitive**. The old guard ignored unknown
codes (allow-by-default); the new guard parks them (deny-by-default). This is the structural core of
the inversion: the guard no longer needs to *recognise* a risk to hold it.

### 3. Stage ordering — topics-first → graph(k-hop) → Chow abstain

1. **Topics-first (pure, no graph).** A member is sensitive iff
   `topic_codes & registry.topic.RISKS` is non-empty OR any code is off-ontology (Decision 2). Closes
   the headline G6 (the 18 missed codes) with zero graph dependency.
2. **k-hop graph sensitivity (Neo4j).** A member with no risk topic of its own is sensitive if a
   **risk-labelled node lies within `k` hops** (ftmg encodes topics as node labels —
   `graph/gds.py`/`generate_topic_labels`). Closes the edge-less / structural fail-open. The
   pipeline threads its existing `Neo4jClient` (`pipeline.py:79`) into `needs_review` at the single
   call site `pipeline.py:357` (keyword-only `neo4j=`, defaulting `None` so pure unit tests skip
   Stage 2). `k` comes from config (`sensitivity_khop_depth`, `int`, `ge=0`), is **validated as an
   int and f-string-INLINED** into the `[*1..k]` variable-length bound (Neo4j forbids a `$param`
   there; inlining a validated int keeps `execute_read`'s `LiteralString` cast sound). The matched
   durable id is a `$param` (it is data, never interpolated).
3. **Chow (1970) abstain band.** A cluster that survives Stages 1-2 is not provably benign if its
   match confidence is marginal. Apply a **reject-option band** (Chow, *On optimum recognition error
   and reject tradeoff*, IEEE Trans. IT 1970) over the cluster's already-computed
   `ResolvedCluster.score`: a score in `[abstain_low, abstain_high)` routes to review. This is the
   **park-vs-auto-merge axis on an already-formed cluster** — explicitly NOT the merge-vs-no-merge
   axis. It **MUST NOT** touch `DEFAULT_MERGE_THRESHOLD = 0.92` (`merge.py:34`) or any Splink weight.

The first stage that flags wins; the same `pending_review` → ADR-0031 sign-off sink is reused (no new
sink/status/table). The guard can only ADD to the parked set.

### 4. `:Ghost` exclusion (Gate D / ADR 0046)

A `:Ghost` endpoint (no anchor props, structurally inert, a never-ingested traversal target) **MUST
NEVER count as a sensitivity or corroboration signal.** The Stage-2 traversal excludes ghosts
(`AND NOT n:Ghost`) and **does not bridge through them** (terminate-at, never traverse-through — a
ghost is not evidence). This preserves ADR 0046's `:Ghost`-exclusion invariant: a ghost is never an
anchor, merge survivor, corroborator, or — now — a sensitivity signal.

### 5. Approved-group-exemption fix (the fail-closed fence)

The exemption at `pipeline.py:359-360` un-flags a cluster ⊆ an approved group. With deny-by-default
now flagging clusters the old denylist missed (e.g. a `role.rca` member), a cluster ⊆ a **stale
approval recorded BEFORE that topic was understood sensitive** would be **silently un-flagged →
auto-merged** — the one path where fail-closed could accidentally NOT park.

**Chosen fix — "re-review a newly-detected sensitivity once" (the user's decision; choice A,
reason-scoped to legacy-visibility).** A sensitivity flag is exemptible by a prior approval **only
if that sensitivity was already visible to the legacy guard at approval time**. Concretely, a
**NEWLY-BROADENED** sensitivity — one the legacy denylist + `role.pep*`/`sanction*` prefix rule
MISSED (the 18 codes, e.g. `role.rca` / `crime.war`, or any off-ontology code) — is **not
exemptible**: a sign-off approving *"these records are the same entity"* could not have considered a
risk it never saw, so the cluster re-parks for a fresh human look. A sensitivity the legacy guard
**ALREADY CAUGHT** (e.g. `sanction`) **was** visible at approval time and **stays exemptible**,
exactly as before — as do the **size** flag (`MAX_AUTO_MERGE_SIZE`) and the **anchor-conflict** flag
(ADR 0040).

This is mechanically scoped to legacy-visibility via `guard.sensitivity.is_newly_broadened_sensitive`
(which replays the *deleted* denylist ONLY to model "what an approving human could already have
understood" — it is NOT the sensitivity decision; that remains the programmatic `registry.topic.RISKS`
path in `is_sensitive`). The fix makes the net STRICTER, never looser, and does not weaken the
existing approve-to-promote path for either a non-sensitive merge or a legacy-caught-sensitive merge
that was knowingly approved (preserving `test_signoff`'s frozen approve→promote contract).

**Why "once," and the known re-ingest property.** The override keys on legacy-visibility, NOT on
"this exact cluster was ever approved," so the fence fires for a newly-broadened-sensitive cluster
**on every RE-INGEST / re-resolution**, not just the first time. This is the deliberate, conservative
fail-closed posture — a re-ingested sanctioned/criminal entity earns a fresh look each time it
re-forms — and is documented as a KNOWN PROPERTY (intended, not a bug) on
`is_newly_broadened_sensitive` and at the `pipeline.py` call site. "Once" describes the per-decision
contract (after a human knowingly approves *this* newly-detected sensitivity, that approval is no
longer stale w.r.t. that risk for that decision); eliminating the re-ingest re-park entirely would
require recording per-approval review rationale (the heavier reason-scoped variant below),
deliberately deferred. (Alternative considered: a fuller reason-scoped exemption keyed on exactly
what each approval reviewed — rejected as heavier, requiring approvals to persist their review
rationale; deferred unless the legacy-visibility heuristic proves too coarse in the field.)

#### Post-review refinement (slice-3, 2026-06-26) — STRUCTURED non-exemptibility replaces the reason-string coupling

An adversarial verification of slice-2 (`c26570d`, APPROVE_WITH_NITS) surfaced a **short-circuit
masking fail-open** in the slice-2 wiring of this fence (gate spec §15.1). slice-2 derived
non-exemptibility from **(1)** `is_newly_broadened_sensitive` over the members (TOPIC-only — correct)
**and (2)** `is_nonexemptible_reason(reason)`, a **substring match of the single reason** that
`needs_review` returns. Because `needs_review` short-circuits on the FIRST flag (`size>10` → topic →
anchor-conflict → k-hop → Chow), an **exemptible** flag firing first (`size>10`, anchor-conflict, or a
**legacy-caught** topic like `sanction`) masked a co-occurring **non-exemptible** Stage-2 k-hop or
Stage-3 Chow signal → a cluster ⊆ a stale approval could be silently un-flagged and auto-promoted
despite real risk-adjacency / marginal confidence — contradicting this Decision's intent and the
fence's own code comment.

**slice-3 replaces the reason-string coupling with a structured probe.**
`guard.sensitivity.has_nonexemptible_sensitivity(cluster, by_id, *, neo4j=None)` returns `True` iff
**any** of the three non-exemptible conditions holds, **each evaluated independently of the
short-circuit**: a member `is_newly_broadened_sensitive` (Stage-1 newly-broadened topic), **or**
(`neo4j is not None and sensitivity_khop_depth > 0` and) a member `_risk_within_khop` (Stage-2 graph
proximity), **or** the cluster `score` is in the Chow band (Stage-3). The pipeline computes the fence
from this probe (recommended: lazily, only on the exemption path, so the common case adds no graph
read), not from the reason. `is_nonexemptible_reason` and the `_KHOP_REASON_MARKER` /
`_ABSTAIN_REASON_MARKER` constants are **deleted** (Finding F: they substring-matched English markers
inside a free-text reason that also embeds hostile data-bearing fields — `member_id`, anchor VALUES);
the human-readable `reason` from `needs_review` stays as the audit reason. This is **monotonically
STRICTER** (more clusters non-exemptible ⇒ more parks) — still person-NEUTRAL / fail-closed, no
sign-off — and **preserves both frozen T4 cases**: `role.rca` (newly-broadened) ⊆ stale approval
re-parks; legacy-caught `sanction` ⊆ approval still auto-promotes (no risk node seeded ⇒ k-hop False;
band OFF ⇒ Chow False; not newly-broadened). DENY **E-MASK** (a facet of E-STALE-EXEMPT) if an
exemptible-first flag masks a co-occurring non-exemptible signal.

### 6. Config (pydantic `BaseSettings`, env-driven; NO YAML)

`sensitivity_khop_depth` (`int`, `ge=0`, **`le=4`** — slice-3 ceiling; default `1`; `0` disables
Stage 2), `sensitivity_abstain_low`/`sensitivity_abstain_high` (`float [0,1]`, default `0.92`/`0.92`
⇒ band OFF until tuned). The risk SET is NOT configured. **No config field can remove a `RISKS` code**
(deny-by-default cannot be configured open).

- **`sensitivity_khop_depth` operational ceiling (slice-3).** The depth is f-string-INLINED into the
  `[*1..k]` variable-length bound, and a var-length traversal is **exponential in `k`**; the field is
  bounded `le=4` so a misconfiguration cannot launch an unbounded graph scan in the resolve hot path.
  `ge=0` is unchanged (`0` is the Stage-2 kill-switch); the default `1` is unchanged.
- **`sensitivity_extra_topics` (WM-070) — DEFERRED, NOT SHIPPED (slice-3 reconciliation).** The
  earlier draft of this Decision and gate spec §6 listed a UNION-only `sensitivity_extra_topics`
  extension surface. It was **never implemented** in `settings.py`; slice-3 reconciles the ADR to the
  shipped code by marking it **DEFERRED**. The deny-by-default set is therefore **exactly**
  `registry.topic.RISKS` + unknown⇒sensitive, with **no config UNION surface at all** — so
  E-CONFIG-OPEN is trivially held (no config field touches the sensitive set). If a CTI/crypto
  enricher later needs to mark its own vocabulary risky, WM-070 reintroduces a UNION-only field
  (never a SUBTRACT) in a dedicated slice.

## Alternatives considered

- **Move `SENSITIVE_TOPICS` to a bigger config list (ADR 0020's deferred plan).** Rejected: still a
  denylist — fails open for the next vocabulary. The programmatic `registry.topic.RISKS` + unknown ⇒
  sensitive is the only allow-list-shaped (deny-by-default) option and tracks FtM upstream for free.
- **Graph-only sensitivity (drop the topic read, use k-hop alone).** Rejected: an entity with a risk
  topic but no graph node (not yet written, or a parked cluster) would escape — Stage 1 must run
  first and unconditionally. (Gate spec DENY E-GRAPHONLY.)
- **Use the Chow band as the merge threshold.** Rejected: that is the merge-vs-no-merge axis owned by
  Splink + `DEFAULT_MERGE_THRESHOLD`; conflating them would re-tune ER and is person-affecting. The
  abstain band is a distinct park-vs-promote axis on an already-formed cluster.
- **Reason-string coupling for the fence (slice-2's `is_nonexemptible_reason`).** Superseded by the
  slice-3 structured probe (Decision 5 refinement). Rejected because the short-circuit in
  `needs_review` made the single returned reason an incomplete view of the cluster's non-exemptible
  signals (the masking fail-open), and because substring-matching a free-text reason that embeds
  hostile data fields is brittle (Finding F).
- **Fuller reason-scoped exemption keyed on per-approval review rationale** (instead of the chosen
  legacy-visibility scoping). Deferred (Decision 5) — heavier (requires approvals to persist what
  they reviewed) and unnecessary; the legacy-visibility heuristic gives the same conservative
  re-review of a newly-detected sensitivity without a schema change.
- **Blanket "any sensitivity overrides the exemption" (no legacy-visibility scoping).** Rejected: it
  would re-park a `sanction` merge a human had ALREADY knowingly approved, breaking the frozen
  `test_signoff` approve→promote contract for legacy-caught sensitivity — over-strict, not the
  user's "re-review a NEWLY-detected sensitivity" decision.
- **G5 (size boundary) fold-in.** Deferred to WM-074 (gate spec §13): the size guard is
  conservative-by-default and does not fail open; folding it dilutes the gate's single message.

## Consequences

- ✅ Closes G6: all 28 FtM risk topics (and any off-ontology code) hold for review; the edge-less /
  structural fail-open closes via k-hop.
- ✅ Strengthens the catastrophic-merge net; auto-promotes nothing; person-NEUTRAL, no sign-off.
- ✅ Tracks FtM's risk vocabulary automatically — a topic added upstream is covered without a code
  change (the adversarial target).
- ✅ (slice-3) The exemption fence is **masking-proof**: non-exemptibility is computed structurally
  (`has_nonexemptible_sensitivity`), independent of `needs_review`'s first-flag short-circuit, so an
  exemptible-first flag can no longer hide a co-occurring k-hop/Chow/newly-broadened signal.
- ⚠️ More clusters route to `pending_review` (the intended trade): the sign-off queue grows. The
  Chow band ships OFF by default and `k` is tunable (`0` disables Stage 2, `le=4` ceiling) so the
  operator controls the volume.
- ⚠️ Stage 2 adds a per-cluster Neo4j read in the resolve path; bounded by `k` and skipped when
  `neo4j is None` or `k == 0`. The slice-3 fence runs the k-hop probe only on the exemption path
  (flagged ∧ ⊆ an approved group), so the common case adds no extra read.
- ⚠️ The exemption fix means a previously approved merge that is NEWLY-BROADENED-sensitive (flagged
  for a risk the legacy guard MISSED, OR k-hop-adjacent, OR Chow-in-band) re-enters review against the
  now-understood risk; a legacy-caught-sensitive merge that was knowingly approved still auto-promotes.
  KNOWN PROPERTY: a newly-broadened-sensitive cluster re-parks on every re-ingest (the fence keys on
  legacy-visibility / structural signal, not "was ever approved") — intended fail-closed conservatism,
  not a bug.

## Status of related ADRs

- **ADR 0020** — the **denylist + evaluation** half is **inverted** by this ADR (note added to 0020).
  The **size** half (`MAX_AUTO_MERGE_SIZE`, G5) remains in force.
- **ADR 0024 / 0031** — `MERGE_GUARD_MODE` action + the `pending_review` → sign-off sink are reused
  unchanged.
- **ADR 0040** — the anchor-conflict park is preserved; it remains exemptible by an approved group.
- **ADR 0046** — the `:Ghost`-exclusion invariant is extended to sensitivity.
