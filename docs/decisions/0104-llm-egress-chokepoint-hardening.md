# 0104 — LLM-egress choke-point hardening (L1): make the advisory-sovereignty model honest, non-bypassable, and auditable

- **Status:** ACCEPTED (2026-07-05)
- **Date:** 2026-07-05
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** n/a — the L1 diff is **non-person-affecting egress-hardening; no `resolution/**` or person-affecting
  path is touched.** L1 changes NO ER threshold, NO merge/guard decision, NO score, NO erasure, and NO gold label.
  It *tightens* the LLM-egress control surface (choke-point contract, mandatory audit for external modes, honest
  caller attribution, an explicit egress role) and touches only `llm/**`, `api/llm.py`, `api/deps.py`, a new
  `authz/roles.py`, `settings.py`, and their tests. Per the ADR-0097 cosign rule and the Gate-1a precedent (ADR 0103),
  the cosign header **tracks the actual diff**; this diff never enters the person-affecting write path, so no cosign is
  required. (Rigorous argument in §"Person-affecting analysis".)
- **Realises:** the L1 hardening items of the Fable LLM/Hermes architecture review (2026-07-05). **Builds on:**
  ADR 0091 (the three-mode confidential selector + `EgressRecord`/`emit` egress audit — the substrate this hardens),
  ADR 0092 (the `POST /v1/chat/completions` HTTP shim + `get_llm_gateway` DI seam), ADR 0090 (the MCP
  bearer-auth + `worldmonitor:graph-read` role→scope map this borrows the role-gate pattern from), ADR 0094 D2
  (sovereignty is an **operator-discretion mode**, not a zero-egress claim — the decision that makes L1, not L2,
  the right gate). **Supersedes:** nothing. ADR 0091's locked three-mode registry (per-mode `data_left_perimeter`
  / confidentiality labels) is **unchanged** by this ADR; L1 only *reads* it.

## Context

The Fable LLM/Hermes architecture review (2026-07-05) found that LLM **sovereignty is enforced on ROUTING but not
on PAYLOAD/POLICY**. Concretely, verified against the tree at `a50c078`:

1. **The choke-point is a docstring, not a contract.** `llm/gateway.py:13` and `api/llm.py:9` *claim* "the gateway is
   the ONLY LLM egress surface", but the only machine-checked fact is that the single real `litellm.completion` call
   lives at `gateway.py:104`. The only actual external-SDK import sites in `src/worldmonitor` are `import litellm` in
   `llm/gateway.py:22` and `llm/claude_shim.py:18-19` (both **inside** `llm/`); there are **zero** `openai`/`anthropic`
   imports anywhere. Nothing stops a future plugin from adding `import litellm` / `import openai` / `import anthropic`
   outside `llm/` and egressing directly — it would pass ruff, pyright, and the existing tests, with **no audit**.
2. **Token-usage audit bug (HIGH).** `gateway.py:83-93` emits one `EgressRecord` **before** the provider call
   (correct — completeness under INV-S2-EGRESS); `usage` is attached to that same record at `:110-114` **after** the
   call, but the log line was already written and is never re-emitted, **and** `egress_log.emit` (`egress_log.py:49-65`)
   does not even serialize `usage` in its line or `extra=` — although the `EgressRecord` docstring lists `usage` as a
   logged field. The token spend of an external call therefore never lands in the audit.
3. **The audit is operator-disableable even for external egress.** `gateway.py:92` gates the emit on
   `settings.llm_egress_log_enabled` (default `True`, `settings.py:304`). An operator can set it `False` and still
   egress to Anthropic/OpenRouter with **zero** audit.
4. **Caller attribution is a hardcoded lie.** `api/llm.py:128` passes `caller_tag="hermes"` for **all** `/v1` traffic;
   the authenticated principal is available at `api/llm.py:86` (`_principal`) but unused. The audit cannot name *who*
   crossed the perimeter.
5. **`/v1` is under-gated relative to the MCP read tools.** `api/llm.py:84` gates on `Depends(get_principal)` = **any**
   valid token (`deps.py:48-59`). The MCP read tools require the `worldmonitor:graph-read` **role**
   (`mcp/auth.py:37,65,75-78`). Spending external LLM egress budget is a *stronger* authority than reading the graph,
   yet it is gated more weakly. The REST layer has **no** role-checking infrastructure today.

**The decision that scopes this gate: F1 = advisory operator-discretion (user, 2026-07-05; ADR 0094 D2).** The review
offered a fork between an **enforced-classification egress gate** (block payloads by data classification — call this
"L2") and an **advisory operator-discretion** model. The user chose **advisory-discretion**: the operator decides,
per the ADR-0094 D2 posture that "sovereignty is an operator-discretion mode" and the zero-egress *claim* is dropped.

Under advisory-discretion, **the audit IS the accountability mechanism.** If the operator (not a classifier) decides
what may leave the perimeter, then a *trustworthy, non-disableable, honestly-attributed* audit is the entire point —
it is what makes the advisory model reviewable after the fact. L1 is exactly the **non-person-affecting hardening**
that makes the advisory model honest and non-bypassable. **L2 (the enforced-classification gate) is explicitly OUT of
scope** and is not specced here (see §Deferred).

## Decision

Ship the five L1 hardening items as **two independent, individually-mergeable builder slices**, both branched from
`origin/master`, both non-person-affecting:

- **Slice L1-a — egress audit + choke-point (items 1, 2, 3).** Touches `llm/gateway.py`, `llm/egress_log.py`,
  `settings.py` (comment-only, if at all), a new repo-root architecture test, and property/unit tests. This is the
  "make the audit honest and the choke-point real" backbone.
- **Slice L1-b — caller attribution + role-gate (items 4, 5).** Touches `api/llm.py`, `api/deps.py`, a new
  `authz/roles.py`, and property/unit tests. This is the "name who egressed and gate who may" auth surface.

The exact file lists, invariants, and acceptance criteria are in `docs/reviews/GATE_L1_LLM_EGRESS_HARDENING_SPEC.md`.

### The five hardening decisions

1. **Machine-enforced choke-point contract.** A test asserts that **no module under `src/worldmonitor` outside the
   `llm/` package imports an external LLM SDK** (`litellm`, `openai`, `anthropic`, in any `import`/`from … import`
   form). The `llm/` package is the sole allowlisted home for provider SDKs. A new external-SDK import anywhere else
   turns the build RED.
2. **Post-call usage record + `emit` serializes usage.** Keep the pre-call completeness emit unchanged
   (INV-S2-EGRESS). After a *successful* provider call, enrich the record's `usage` in place **and emit a second
   record** carrying it; teach `egress_log.emit` to serialize token counts (line + `extra=`) when `usage` is present.
   A failing/timing-out call still emits exactly the one pre-call record (attempt audited, no usage).
3. **External egress requires audit — fail-closed.** When the resolved mode has `data_left_perimeter == True`
   (external) **and** `llm_egress_log_enabled` is `False`, the gateway **refuses the call** (raises
   `LLMGatewayError`) and `litellm.completion` never fires. LOCAL (`data_left_perimeter == False`) remains freely
   toggle-able. No durable audit ⇒ no external egress.
4. **Honest caller attribution.** `api/llm.py` derives `caller_tag` from `_principal.subject` (the authenticated
   Zitadel subject), falling back to a sensible default only when no subject is present. The audit then names who
   crossed the perimeter.
5. **A dedicated egress role gates `/v1`.** `POST /v1/chat/completions` requires an explicit
   `worldmonitor:llm` project role; a valid token lacking it gets **403** and the gateway is never called.

### The four sub-forks — resolved (reversible defaults; reversal cost + revisit trigger recorded)

**SF-1 · Choke-point mechanism → pytest AST architecture test (not import-linter).**
The contract is a repo-root pytest that walks the `ast` of every `.py` under `src/worldmonitor` (excluding the `llm/`
package) and flags any `import`/`from` whose top-level module is in `{litellm, openai, anthropic}`.
*Why:* it adds **no dependency** and **no new CI step** — it runs inside the existing `quality` job under
`pytest -m "not integration"`, exactly like the repo's existing structural guards (`tests/test_contract_consistency.py`,
`tests/test_ftm_schema_vendored.py`). It **must use AST, not text grep**, because `api/llm.py`, `settings.py`, and
`modes.py` mention "litellm" in docstrings/comments — a grep would false-positive. Rejected: an `import-linter`
`[importlinter]` contract in `pyproject.toml` — it adds a runtime/dev dependency, a separate CI invocation, and a
config surface not covered by the current gate.
*Reversal cost:* **LOW** — swap in an import-linter contract (~½ day); the AST test can coexist.
*Revisit trigger:* the AST test yields a real false-positive/negative (e.g. an aliased or `importlib.import_module`
dynamic import it misses — SF-1 mitigates this with a secondary constant-string dynamic-import check), **or** we want
enforcement at commit/lint time rather than test time.

**SF-2 · External-egress-without-audit → fail-closed-refuse (not ignore-the-flag / force-emit).**
*Why:* under advisory-discretion the audit is the accountability backbone; "no durable audit ⇒ no egress" is the most
legible contract and never silently overrides an operator's explicit config. Force-emitting despite a disable flag is
weaker: it surprises an operator who disabled logging (perhaps because the sink is broken) by egressing anyway and
writing to a sink they believe is off. LOCAL stays toggle-able because disabling a *confidential, on-perimeter* call's
audit affects no external accountability.
*Reversal cost:* **LOW** — flip the `raise` to a force-emit in one branch.
*Revisit trigger:* an operational need to egress externally without the stdlib-logging sink — which would itself be
the F2 durable-audit redesign (deferred), where the audit sink changes shape.

**SF-3 · `/v1` authority → a dedicated `worldmonitor:llm` role (not reuse `worldmonitor:graph-read`).**
*Why:* reading the resolved graph and spending external LLM egress are **different authorities** (least privilege); a
graph-read principal should not automatically be able to burn external egress budget. The MCP tools require
`worldmonitor:graph-read`; `/v1` requires `worldmonitor:llm`. **Docs-only provisioning implication:** the Hermes
service-principal must be granted the new `worldmonitor:llm` project role in Zitadel — a documented operational step,
no runtime code that provisions it, no new runtime dependency. Because the only current `/v1` caller (Hermes, ADR 0093)
is **not yet host-operational** (L4 deferred), introducing a required role now causes **no live regression**: the
operator grants the role as part of enabling Hermes.
*Reversal cost:* **LOW-MODERATE** — collapsing to `graph-read` deletes the role constant + one dependency; a role
already provisioned in Zitadel is harmless to leave in place.
*Revisit trigger:* role proliferation becomes an ops burden, or a second `/v1`-class endpoint appears and a coarser
"agent" role is warranted.

**SF-4 · Slicing → two slices (not one PR).**
*Why:* the five items split cleanly along two surfaces with different review focus — an **audit/egress** surface
(`llm/**` + settings, slice L1-a) and an **auth/role** surface (`api/**` + `authz/**`, slice L1-b) — with **no code
dependency** between them (L1-a never touches `api/llm.py`; L1-b never touches the gateway). Two small focused PRs match
the repo grain (1a/1b, 2a/2b, 3a-i/ii) and CLAUDE.md's "one focused feature per PR". L1-a is the first slice (the
audit backbone the review calls the point of L1) and has zero cross-test conflict; L1-b follows with its own
`.claude/gate.scope`.
*Reversal cost:* **TRIVIAL** — a process choice, not a code lock-in.
*Revisit trigger:* if L1-a review shows the two surfaces are entangled, fold into one PR (analysis says they are not).

## Person-affecting analysis (why `person_affecting: false`, no cosign)

The ADR-0097/CLAUDE.md rule requires human sign-off for changes that affect a real person — ER thresholds, merge/guard
decisions, individual-affecting scores, erasure. L1 touches **none** of them:

- **No resolution write path.** The diff does not touch `resolution/**` (clustering, thresholds, `signoff.py`,
  `guard.py`/sensitivity, `gold.py`, `eval.py`, `pipeline.py`, `statements.py`, `projector.py`), `graph/writer.py`, or
  `db/models.py`/`db/migrations/**`. No merge is created, blocked, approved, or rejected; no gold label is written; no
  calibration threshold or EM weight moves.
- **It only tightens an egress control + audit.** Every change makes external LLM egress *harder to do unaudited*
  (choke-point contract, fail-closed audit), *more honestly attributed* (real caller), and *more tightly gated*
  (a dedicated role). Tightening a control that governs *model calls* is not a person-affecting resolution change.
- **It does not implement L2.** The enforced-classification gate (which *would* make a payload-level policy call about
  what data may leave) is dropped per F1 and is not built here.

The diff footprint is `llm/gateway.py`, `llm/egress_log.py`, `api/llm.py`, `api/deps.py`, `authz/roles.py` (new),
`settings.py`, tests, and one CI/test contract — **not** `resolution/**`. As with Gate 1a (ADR 0103), the cosign header
tracks the actual diff: `person_affecting: false`, `human_cosign: n/a`. This is **not** a promissory placeholder — there
is no deferred person-affecting behaviour hiding behind L1; the person-affecting egress *policy* fork (L2) is closed as
out-of-scope, not postponed.

## Alternatives considered

- **Enforced-classification egress gate (L2).** Rejected/out-of-scope per **F1 = advisory operator-discretion**
  (ADR 0094 D2). A classifier that blocks payloads by data classification *is* a person-affecting policy engine; the
  user chose operator-discretion, so the audit — not a classifier — is the accountability mechanism.
- **Text-grep choke-point check.** Rejected (SF-1): false-positives on docstring/comment mentions of "litellm".
- **Force-emit the audit despite the disable flag.** Rejected (SF-2): silently overrides operator config; weaker than
  fail-closed-refuse.
- **Reuse `worldmonitor:graph-read` for `/v1`.** Rejected (SF-3): conflates two distinct authorities.
- **One combined PR.** Rejected (SF-4): two clean, independent surfaces; two focused PRs match the repo grain.

## Consequences

- The "one LLM egress path" claim becomes a **machine-checked contract**, not a docstring; a stray external-SDK import
  outside `llm/` fails CI.
- External LLM egress is **impossible without a durable, usage-bearing, honestly-attributed audit record** — the
  accountability backbone the advisory model needs.
- `/v1` now requires the `worldmonitor:llm` role. **Operational note (docs-only):** grant that role to the Hermes
  service-principal in Zitadel before enabling the S4/S5 Hermes path; until then no live caller is affected.
- Two existing tests must be updated **in-slice** to reflect the new contracts (this is expected, not scope creep):
  L1-a updates `tests/unit/test_llm_gateway.py`'s success-path emit-count assertion (one → two emits); L1-b updates
  the fake verifiers in `tests/unit/test_api_llm_endpoint.py` and `tests/property/test_api_llm_gateway_delegation.py`
  to grant the new role so their 200-expecting cases stay green under the role gate. The existing property
  `tests/property/test_llm_egress_completeness.py` stays green **unchanged** (it reads `captured[0]` and uses
  `events.index(...)` = first occurrence, both of which survive a second post-call emit).

## Deferred / out-of-scope (spec none of these here)

- **L2 — enforced-classification egress gate: DROPPED** per F1 = advisory operator-discretion (ADR 0094 D2). Not a
  postponement; the person-affecting egress-policy fork is closed in favour of the audit-as-accountability model.
- **F2 — durable, append-only egress audit** (content-fingerprint + entity-manifest, moving off stdlib logging to a
  tamper-evident store): **backlog.** L1's fail-closed + usage record is the honest-*today* step; F2 is the durable
  substrate.
- **F4 — the Phase-6 self-improvement gate** (agent-driven change → propose/evaluate/gate/promote): **backlog**, Phase 6.
- **L3 — make LOCAL real** (a working Ollama-backed confidential default rather than a configured-but-unverified mode):
  **backlog.**
- **L4 — unblock the Hermes run** (host-operational S4/S5): **backlog**, blocked on the user's deploy-and-verify step.
