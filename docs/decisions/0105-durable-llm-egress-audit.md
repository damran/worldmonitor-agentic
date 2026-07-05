# 0105 — Durable, append-only LLM-egress audit (F2): move the accountability record off ephemeral stdlib logging onto a tamper-evident Postgres spine

- **Status:** PROPOSED (2026-07-05)
- **Date:** 2026-07-05
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** n/a — F2 is **non-person-affecting egress-audit substrate**. It adds an append-only
  LLM-egress audit table + writer + gateway wiring; it changes **no** ER threshold, **no** merge/guard
  decision, **no** individual-affecting score, **no** erasure, and **no** gold label. It touches
  `llm/**`, one new `db/models.py` model (**additive only** — every existing model byte-unchanged), one
  new migration, `settings.py` (one additive flag), and `api/main.py` (one-line gateway construction).
  It does **not** touch `resolution/**`, `graph/writer.py`, or the person-affecting `StatementRecord` /
  `DecisionRecord` / `MergeAudit` / `SignOff` / `ErGoldPair` models. Per the ADR-0097 cosign rule and
  the Gate-1a/L1 precedent (ADR 0103, 0104), the cosign header **tracks the actual diff**; this diff
  never enters the person-affecting write path, so no cosign is required. (Rigorous argument, including
  the `db/models.py` additive touch, in §"Person-affecting analysis".)
- **Realises:** the **F2 — durable, append-only egress audit** deferred item of ADR 0104 §Deferred
  ("content-fingerprint + entity-manifest, moving off stdlib logging to a tamper-evident store").
  **Builds on:** ADR 0104 (the L1 hardening this makes durable — the fail-closed audit obligation, the
  usage record, the honest caller attribution, the machine-enforced choke point), ADR 0091 (the
  `EgressRecord` / `emit` stdlib-logging audit substrate this sits **alongside**, unchanged), ADR 0099
  (the append-only statement/decision **spine idiom** — INSERT-only `session.add` writers, model +
  migration byte-agreement per ADR 0030, `scope` reserved-column discipline), ADR 0100 (whose `seq`
  IDENTITY column + dialect-guarded `before_insert` SQLite listener is the **known trap** this ADR
  deliberately avoids), ADR 0092 (the `get_llm_gateway` DI seam). **Supersedes:** nothing. ADR 0104's
  L1 behaviour (choke point, fail-closed-on-flag, usage record, caller attribution, role gate) is
  **unchanged**; F2 is additive and **dormant by default**.

## Context

Under **F1 = advisory operator-discretion** (user, 2026-07-05; ADR 0094 D2; ADR 0104 Context), LLM
sovereignty is enforced on **routing**, not on payload/policy. There is no classifier that blocks what
leaves the perimeter — the operator decides. ADR 0104 draws the direct consequence: **the audit IS the
accountability mechanism.** If a classifier does not decide what may egress, then a *trustworthy,
non-disableable, honestly-attributed* audit is the entire point — it is what makes the advisory model
reviewable after the fact.

L1 (ADR 0104) made that audit **honest today**: a fail-closed obligation (external egress refused when
the audit is disabled), a post-call usage record, an honestly-attributed `caller_tag`, and a
machine-enforced choke point. But L1's audit sink is still **`egress_log.emit()` → stdlib `logging`**
(`llm/egress_log.py`). That record is **ephemeral**: a log rotation, a dropped handler, or process
death loses the accountability record. Under advisory-discretion, losing the audit means losing the
*only* record that a given payload crossed the perimeter, by whom. That is the gap F2 closes.

F2 adds a **durable, append-only Postgres audit** written by the same single gateway choke point, at
the same two points as the stdlib emit (pre-call attempt, post-call usage). It carries the
review's "**what/whose**" enrichment — a **content fingerprint** (a deterministic digest over the
canonicalized outbound messages) and an optional **entity manifest** (a caller-declared list of
canonical entity ids in the payload) — so the audit records not just *that* an egress happened and
*how much* it cost, but a tamper-evident fingerprint of *what* crossed and *whose* data the caller
declared was in it. It **never** stores the message content itself, and **never** the api key.

F2 ships **dormant** (a new default-off flag). The stdlib `emit()` path (L1's honest-today posture) is
byte-unchanged and continues to fire; the durable table is additive substrate the operator enables
once a Postgres sink is verified — exactly the repo's dormant-substrate pattern (the statement-spine
dual-write, ADR 0099; the projector, ADR 0100; the rebuild-diff guard, ADR 0102).

## Decision

Ship F2 as **one** independent, individually-mergeable builder slice, branched from `origin/master` as
`gate/f2-durable-egress-audit`, non-person-affecting.

1. **New append-only Postgres table `llm_egress`** (model `LlmEgressRecord` in `db/models.py`), in the
   ADR-0099 spine idiom: **INSERT-only** writer, no UPDATE / DELETE ever, `String(64)` UUID primary key,
   model + migration (`0011_llm_egress_audit`) byte-identical (ADR 0030 drift guard). **No `seq`
   IDENTITY column** (see SF-3): nothing consumes an ordering watermark here, and ADR 0100's
   `seq`/SQLite regression is a known trap — `created_at` + `call_id` suffice for ordering display and
   pre/post correlation. Two row kinds per crossing, distinguished by `phase` and correlated by a
   shared `call_id`: an **attempt** row (pre-call: `content_fingerprint` + `entity_manifest`, no usage)
   and a **completed** row (post-call: token usage, no fingerprint).

2. **Auditability bar = "what/whose", not just "that/how-much".** Each **attempt** row carries
   (a) a **content fingerprint** — `sha256` over a canonical-JSON serialization of the outbound
   `messages` (SF-1: plain `sha256`, not HMAC), a fixed-length 64-char hex digest; and (b) an
   **entity manifest** — an OPTIONAL, **caller-declared** list of canonical entity ids whose data the
   caller asserts is in the payload (SF-2: caller-declared, not content-derived; nullable JSONB). A new
   optional `entity_ids: list[str] | None = None` kwarg on `LLMGateway.chat()` carries it; service-side
   callers may declare it; `/v1` wire traffic passes `None` (recorded honestly). **Never** the message
   content; **never** the api key.

3. **Fail-closed coupling extends INV-FAILCLOSED (ADR 0104 item 3).** When the durable obligation is
   active (`llm_egress_log_enabled AND llm_egress_durable_enabled`) and the mode is **external**
   (`data_left_perimeter is True`): the durable **attempt** row INSERT+commit must **succeed before**
   `litellm.completion` fires. A DB-unreachable / write-failed / unwired-sink condition ⇒ refuse the
   external call with `LLMGatewayError` (no durable audit ⇒ no egress). **LOCAL** mode: the durable
   write is attempted **best-effort** — a sink failure logs a warning and the confidential on-perimeter
   call proceeds (SF-5, the asymmetry). Post-call: a **second** row (INSERT, correlated by `call_id`)
   carrying usage tokens — best-effort for both modes — is the append-only analogue of L1's two-emit
   pattern; **no** update-in-place of the attempt row. Provider failure after the attempt row ⇒ exactly
   one row.

4. **The gateway gets a DB seam it lacks today.** `LLMGateway(settings, session_factory=None)` — a
   `sessionmaker` injected at construction; default `None` preserves every existing construction. The
   real wiring lands in `api/main.py` (`LLMGateway(settings, session_factory=db_sessions)`, reusing the
   same `sessionmaker` the review-queue UI reads Postgres through, ADR 0069/0103); `get_llm_gateway`
   (`api/deps.py`) is unchanged (it already reads `app.state.llm_gateway`). A `None` factory + external
   mode + durable obligation active ⇒ refuse (an unwired gateway cannot silently egress unaudited).

5. **Tamper-evidence v1 = append-only posture**, not a hash-chain (SF-6): an INSERT-only writer plus a
   `@given` property test asserting the writer issues no UPDATE/DELETE across arbitrary call sequences,
   plus the documented append-only invariants. A prev-row-digest hash-chain is the deferred revisit path.

6. **The stdlib `emit()` stays exactly as is** (L1 / INV-S2-EGRESS unchanged). The durable table is
   **additive alongside**; both sinks fire at the same two points. `llm/egress_log.py` is FROZEN.

7. **Dormant by default.** New flag `llm_egress_durable_enabled: bool = False`. When `False` (default),
   behaviour is byte-identical to L1 — no durable row, no new refuse condition — so the FROZEN
   completeness property (`tests/property/test_llm_egress_completeness.py`) stays green **unchanged**.

The exact file lists, invariants, and acceptance criteria are in
`docs/reviews/GATE_F2_DURABLE_EGRESS_AUDIT_SPEC.md`.

## The sub-forks — resolved (reversible defaults; reversal cost + revisit trigger recorded)

Per CLAUDE.md build-discipline (ADR 0097 reversibility) and the ADR-0104 SF form, each locked design
direction is recorded as a **decided reversible sub-fork** — a sensible default with its reversal cost
and a revisit trigger. None is a genuine product/architecture fork; there is no OPEN human stop.

**SF-1 · Content fingerprint → plain `sha256` (not HMAC-with-a-server-key).**
The fingerprint is a deterministic `sha256` over the canonical-JSON serialization of the outbound
`messages` (`json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
default=str)` → `sha256` hex — the repo's existing `sort_keys=True` canonicalization idiom, e.g.
`provenance/model.py`, `backup.py`; stdlib only, no new dependency).
*Why:* the fingerprint's job is **content-identity + tamper-evidence** (did *this* payload cross? did
two crossings carry identical content? was a row altered?), not confidentiality of the content. The
store is the self-hosted, single-tenant SoR; a keyed HMAC defends only against an attacker who holds
the DB but not a server key, which is not the threat that motivates F2.
*Known weakness (recorded honestly):* plain `sha256` is **dictionary-checkable** — an adversary with
the DB can confirm a *guess* about a short/guessable payload. Accepted for v1; the content is never
stored, so nothing stronger than a guess-confirmation is exposed.
*Reversal cost:* **MODERATE-forward-only** — switch to HMAC as an **additive** column computed on
**new rows only**; old rows can never be retroactively re-fingerprinted (the content was, by design,
never stored). The plain column stays.
*Revisit trigger:* a threat-model / compliance requirement that the DB-at-rest not be
dictionary-checkable for short payloads, or a requirement for keyed tamper-evidence.

**SF-2 · Entity manifest → caller-declared optional list (not content-derived).**
A nullable JSONB `entity_manifest` populated from a new optional `entity_ids` kwarg the service-side
caller declares; `/v1` wire callers pass `None` (the manifest is **absent** for wire traffic — recorded
honestly, not faked).
*Why:* deriving the manifest from the payload would require a payload entity-recogniser — a classifier
that drifts toward the **enforced-classification gate (L2) that F1 explicitly DROPPED**. A caller
declaration keeps F2 on the advisory-audit side of that line and couples the audit to nothing.
*Reversal cost:* **LOW-forward-only** — stop populating (rows stay, append-only) or start deriving in a
later gate; either way new rows only.
*Revisit trigger:* a need for content-derived provenance that does **not** reintroduce classification,
or evidence that service callers under-declare.

**SF-3 · No `seq` IDENTITY column (created_at + call_id for ordering/correlation).**
Unlike `StatementRecord` / `DecisionRecord` (ADR 0100), `llm_egress` carries **no** monotonic `seq`
IDENTITY.
*Why:* nothing consumes an ordering watermark over the egress log (no projector, no incremental
exporter). ADR 0100's `seq` forced a dialect-guarded `before_insert` SQLite fallback listener (Postgres
IDENTITY is a no-op there) — a real, load-bearing trap that has already produced a regression. Omitting
`seq` avoids the listener entirely; `created_at` (server default) orders rows for display and `call_id`
correlates the attempt/completed pair.
*Reversal cost:* **MODERATE** — adding `seq` later is a migration **plus** re-introducing the
dialect-guarded `before_insert` listener (ADR 0100's exact pattern).
*Revisit trigger:* a real consumer of a monotonic egress-log watermark appears (e.g. an incremental
export/verification job that must resume from a checkpoint).

**SF-4 · One durable flag, defaulting OFF (`llm_egress_durable_enabled=False`) — NOT reusing
`llm_egress_log_enabled` for the durable obligation.**
*Why (the binding constraint):* the durable fail-closed coupling (SF/Decision 3) means external egress
must refuse when the durable sink is unwired/unreachable. If that obligation were governed by the
existing `llm_egress_log_enabled` (default `True`), then the FROZEN completeness property — which drives
external-mode calls with `llm_egress_log_enabled=True` and **no** injected session factory — would start
**refusing** and go RED, and there is **no durable sink operationally verified** in production yet. A
separate default-OFF flag (a) keeps the completeness property byte-unchanged and green, (b) ships the
new substrate **dormant** exactly like the projector (ADR 0100) and the rebuild-diff guard (ADR 0102),
and (c) lets the operator enable durable auditing only after verifying the Postgres sink + applied
migration. The "avoid a matrix of half-audited states" intent still holds: `D` only **tightens** when
`L` is already on — `L=False` ⇒ no egress at all (existing INV-FAILCLOSED); `L=True, D=False` ⇒ exactly
L1's honest-today posture; `L=True, D=True` ⇒ external egress is fail-closed on the durable sink and
both sinks fire. There is no half-audited state.
*Reversal cost:* **LOW** — collapse to one flag by deleting the field and gating the durable obligation
on `llm_egress_log_enabled`; would then require injecting a session factory into the completeness test.
*Revisit trigger:* durable auditing becomes the verified default-on production posture and the team
wants a single audit-obligation flag.

**SF-5 · Fail-closed asymmetry — external = fail-closed durable commit; LOCAL = best-effort.**
*Why:* external egress is the accountability-critical crossing; "no durable audit ⇒ no egress" must hold
for it. A confidential on-perimeter LOCAL call has no external accountability at stake, so an audit-sink
failure must not break it (mirrors L1's LOCAL-stays-toggle-able posture, ADR 0104 SF-2).
*Reversal cost:* **LOW** — make LOCAL fail-closed too (stricter) or external best-effort (weaker) in one
branch.
*Revisit trigger:* an operational need for strict durability across all modes.

**SF-6 · Tamper-evidence v1 = append-only posture (not a hash-chain).**
*Why:* an INSERT-only writer + a property test proving no UPDATE/DELETE is issued gives append-only
tamper-evidence at the writer boundary today, with zero concurrency machinery. A prev-row-digest
hash-chain needs concurrent-writer serialization (two gateway calls racing to append) that is out of
scope for the honest-today step.
*Reversal cost:* **MODERATE** — add a `prev_digest` chain column (additive, new rows) plus a
serialization discipline for concurrent appends.
*Revisit trigger:* an operator requirement for cryptographic (not just posture) tamper-evidence.

**SF-7 · Session seam → constructor injection (`session_factory=None` default).**
*Why:* mirrors the app's DI-for-testability idiom (`neo4j_client`, `db_sessions`, `llm_gateway` are all
injected at `create_app`); `None` default keeps all 15 existing gateway constructions working; tests
inject a spy/SQLite factory; production wires `db_sessions` in `api/main.py`.
*Reversal cost:* **TRIVIAL.**
*Revisit trigger:* the gateway needs richer DB access (unlikely; the writer owns its own short
transaction).

**SF-8 · Slicing → one slice (not split).**
*Why:* the surface is one coherent audit substrate — one table + one writer module + the gateway
control-flow change + one migration + tests — and the pieces are mutually dependent (the model without
the writer is inert; the writer without the gateway call is unreachable; the gateway change without the
table does not compile). There is no clean independent merge seam, and the whole thing is dormant behind
one flag, so a single focused PR matches the repo grain.
*Reversal cost:* **TRIVIAL** — a process choice.
*Revisit trigger:* if the builder finds the model+migration can land and be verified separately from the
gateway wiring, split — analysis says they are one unit.

## Person-affecting analysis (why `person_affecting: false`, no cosign)

The ADR-0097/CLAUDE.md rule requires human sign-off for changes that affect a real person — ER
thresholds, merge/guard decisions, individual-affecting scores, erasure, gold labels. F2 touches
**none** of them:

- **No resolution write path.** The diff does not touch `resolution/**` (clustering, thresholds,
  `signoff.py`, `guard.py`/sensitivity, `gold.py`, `eval.py`, `pipeline.py`, `statements.py`,
  `projector.py`, `merge`/referents/canonical) or `graph/writer.py`. No merge is created, blocked,
  approved, or rejected; no gold label is written; no calibration threshold or EM weight moves.
- **The `db/models.py` touch is additive-and-disjoint.** F2 appends **one** new model, `LlmEgressRecord`
  (table `llm_egress`), and adds `Boolean`/`Integer` to the import line. **Every existing model is
  byte-unchanged** — including the person-affecting `StatementRecord`, `DecisionRecord`, `MergeAudit`,
  `SignOff`, `ErGoldPair`, and the `_assign_sqlite_seq` `before_insert` listener block (untouched
  because the new table has **no** `seq` column, SF-3). The new table records **LLM-call metadata**
  (mode, target host, caller, a content fingerprint, a caller-declared entity manifest, token usage) —
  structurally disjoint from any ER/merge decision. The migration drift guard
  (`tests/integration/test_migrations.py`, ADR 0030) proves the existing tables' schemas are unchanged;
  the merge/statement property suites prove the resolution path is byte-unchanged.
- **It only makes an egress *audit* durable.** Every change makes an external LLM egress *harder to do
  unaudited* (a durable, fail-closed record) and *more precisely attributed* (fingerprint + declared
  manifest). Hardening an audit that governs *model calls* is not a person-affecting resolution change.
- **It does not implement L2.** No payload-level classification / blocking is added; the manifest is a
  caller declaration, not a content-derived policy decision (SF-2).

The diff footprint is `llm/gateway.py`, `llm/egress_audit.py` (new), `db/models.py` (additive model),
one migration, `settings.py` (one flag), `api/main.py` (one construction line), and tests — **not**
`resolution/**`. As with Gate 1a (ADR 0103) and L1 (ADR 0104), the cosign header tracks the actual
diff: `person_affecting: false`, `human_cosign: n/a`. This is **not** a promissory placeholder — there
is no deferred person-affecting behaviour hiding behind F2; the person-affecting egress *policy* fork
(L2) is closed as out-of-scope per F1, not postponed.

## Adversarial-verification findings (5-lens audit, 2026-07-05 — fix round applied pre-merge)

A perspective-diverse adversarial review (fail-closed-bypass / leak+append-only / dormancy+L1 /
test-integrity / spine+governance) ran against the built branch, alongside an independent checker
that reproduced every §7 invariant (PASS). The lenses produced **executed** proofs on real Postgres
and a real FastAPI client — the fifth consecutive gate where this pattern caught defects the test
suite dodged. All fixed in-branch before merge:

- **FIXED (HIGH — mode-bricking column bound).** `llm_egress.confidentiality` was `VARCHAR(64)` but
  the ADR-0091 registry's CLAUDE_HEADLESS confidentiality label is 153 chars — the attempt INSERT
  raised `StringDataRightTruncation`, the fail-closed path (correctly) refused, and **every**
  CLAUDE_HEADLESS crossing was refused whenever durable auditing was on (executed proof). Escaped
  the suite because property tests use spies, unit tests run SQLite, and the Postgres integration
  cases used OPENROUTER only. Fix: `confidentiality` + `caller_tag` (unbounded JWT subject) →
  `Text` in model + migration `0011` (amended in-branch — unmerged, no shipped history rewritten);
  new Postgres integration test drives the REAL durable write for **every** registry mode and
  asserts the full untruncated label round-trips.
- **FIXED (HIGH — fingerprint was not total; raw escape past the typed-error contract).**
  `fingerprint_messages` raised on four executed hostile classes — a lone UTF-16 surrogate
  (**wire-reachable**: stdlib `json.loads` accepts the `\ud800` escape, pydantic `content: str`
  passes it through ⇒ executed HTTP 500 on `/v1`), a circular structure, a leaf whose `__str__`
  raises, and mixed-type dict keys under `sort_keys` — and the build call sat OUTSIDE the guarded
  blocks, so the raw untyped exception escaped `chat()` (breaking the typed-error contract and, on
  LOCAL, the SF-5 best-effort promise). Fix: the fingerprint is now **total** (`surrogatepass`
  encoding + a coarse deterministic type-level sentinel fallback; determinism domain documented
  honestly — byte-deterministic for JSON-shaped wire payloads, type-level otherwise), and the row
  build moved INSIDE the per-mode guarded blocks. New tests: the four executed classes as
  parametrized totality cases + a gateway-level no-untyped-escape test (provider still called,
  fingerprint still 64-hex).
- **FIXED (LOW — silent LOCAL misconfiguration).** Durable-enabled-but-unwired was loud for
  external (fail-closed refuse) yet silent for LOCAL — one construction-time warning added.
- **FIXED (LOW — P-AUDIT-1 oracle decomposition).** The ordering oracle never tied the
  pre-provider commit to the attempt row itself (a contrived commit-empty-then-add-later impl
  passed); the spy session now snapshots each commit's rows and P-AUDIT-1 asserts the
  pre-provider commit carries the attempt row.
- **Recorded, not fixed (LOW observations):** v1 "tamper-evidence" is writer-boundary posture only
  (no DB-level `REVOKE UPDATE/DELETE`/trigger; the SF-6 hash-chain deferral stands — ops docs may
  add a grant-level note when durable auditing is first enabled); the migration drift guard does
  not compare `server_default` (pre-existing gap, later hardening); `default=str` reprs embed
  memory addresses for exotic in-process payloads (documented as the determinism domain, not
  "fixed" — the wire case is unaffected).

## Alternatives considered

- **Keep the audit on stdlib logging only.** Rejected: ephemeral. Under F1 = advisory-discretion the
  audit is the accountability mechanism; a rotation/crash that loses it loses the only record a payload
  crossed the perimeter. F2 is exactly the durable-substrate step ADR 0104 §Deferred names.
- **HMAC-keyed fingerprint in v1.** Rejected for v1 (SF-1): adds server-side key management/rotation for
  a defence against a threat (DB-holder-without-key) that the self-hosted single-tenant deployment does
  not face; deferred as an additive, forward-only revisit path.
- **Content-derived entity manifest.** Rejected (SF-2): requires a payload classifier that drifts toward
  the DROPPED L2 gate.
- **A `seq` IDENTITY ordering column (mirror ADR 0099/0100).** Rejected (SF-3): no consumer, and it
  drags in ADR 0100's dialect-guarded `before_insert` SQLite trap for no benefit.
- **Reuse `llm_egress_log_enabled` for the durable obligation (one flag).** Rejected (SF-4): would turn
  the FROZEN completeness property RED and would flip production external egress to hard-fail before a
  durable sink is verified. Shipped dormant behind a default-off flag instead.
- **A hash-chain tamper-evidence in v1.** Rejected (SF-6): needs concurrent-append serialization out of
  scope for the honest-today step; append-only posture + a no-UPDATE/DELETE property is the v1 bar.
- **Store the message content (or a truncated preview).** Rejected outright: violates the ADR-0091 §3
  no-content / no-key rule; the fingerprint is the durable, non-leaking stand-in.
- **Two slices (table/migration separate from gateway wiring).** Rejected (SF-8): mutually dependent,
  dormant behind one flag; one focused PR.

## Consequences

- The LLM-egress accountability record becomes **durable and tamper-evident** (append-only, INSERT-only)
  instead of ephemeral stdlib logging — the substrate the advisory-discretion model needs.
- Each external crossing is fingerprinted (content-identity, non-leaking) and optionally
  entity-manifested (caller-declared), so the audit answers *what* crossed and *whose* data the caller
  declared, not just *that* it happened and *how much* it cost.
- **Dormant by default** — no behavioural change in production until an operator sets
  `llm_egress_durable_enabled=True` (after applying migration `0011` and confirming the Postgres sink).
  When enabled, external egress is **fail-closed** on the durable sink: DB down ⇒ no external egress.
- **Enablement is bound to the first real egress consumer (anti-drift):** the L4 / Hermes-enablement
  runbook MUST set `llm_egress_durable_enabled=True` as part of bringing the first live `/v1` caller
  up, and the S4 resume-condition proof extends from "an `llm-egress caller=hermes` log line" to "a
  durable `llm_egress` row with `caller_tag=hermes`". The dormant window is a pre-production posture
  only; it does not survive into real operation.
- The gateway gains a `session_factory` seam; `api/main.py` wires the existing `db_sessions`
  `sessionmaker`. No existing gateway construction changes behaviour (default `None` + dormant flag).
- **New drift-guard surface:** the `LlmEgressRecord` model and migration `0011_llm_egress_audit` must
  agree byte-for-byte (`tests/integration/test_migrations.py`, ADR 0030); `_migration_guard`
  auto-applies the lock timeout to the new DDL (ADR 0084) — no builder action needed.
- The stdlib `emit()` audit is unchanged and continues to fire alongside the durable table.

## Deferred / out-of-scope (spec none of these here)

- **Hash-chain tamper-evidence** (prev-row digest chaining) — SF-6 revisit path; needs concurrent-append
  serialization.
- **HMAC-keyed fingerprint** — SF-1 revisit path (additive, forward-only column).
- **Content-derived entity manifest** — SF-2 revisit path (must not reintroduce L2 classification).
- **A `seq` monotonic watermark** — SF-3 revisit path, only if a real egress-log consumer appears.
- **Retention / rotation policy for `llm_egress`.** Append-only means the table grows unbounded; a
  retention policy (age-based archival/pruning under an explicit operator decision, distinct from the
  never-hard-delete reference-set invariant of the ER queue, ADR 0086) is a later gate. F2 makes the
  record durable; how long it is *kept* is an operator/retention decision not made here.
- **An export / verification CLI** (`python -m worldmonitor.llm.egress_audit export|verify`) that streams
  the durable log and re-checks append-only posture / row integrity for an auditor — a later gate.
- **L2 — enforced-classification egress gate:** remains DROPPED per F1 (ADR 0104). F2 audits; it does not
  classify or block by data classification.

## Reversibility

**Reversible** (additive substrate, dormant by default). Reversal cost: drop the table (`downgrade()`),
delete `llm/egress_audit.py`, revert the gateway's durable-write branches + the `session_factory` param,
remove the `llm_egress_durable_enabled` flag, and revert the one `api/main.py` construction line. No
data migration of live graph state, no behaviour to unwind (the flag defaults off; nothing depends on
the table). **Revisit trigger:** any of the SF revisit triggers above, or an operator enabling durable
auditing and then requiring a retention policy / export-verify CLI.

## ADR-index coupling

Adding this file requires the builder to re-run `uv run python scripts/gen_adr_index.py` so
`docs/decisions/README.md` gains the `0105` row (else the `adr-index` CI check goes red). This header
uses the canonical list dialect (`Status`/`Date`/`human_fork`/`person_affecting` on lines 3–6) the
generator parses, so the regenerated row reads `PROPOSED | 2026-07-05 | false | false`. Status flips
PROPOSED → ACCEPTED at merge after the judge APPROVEs (the main-loop convention).
