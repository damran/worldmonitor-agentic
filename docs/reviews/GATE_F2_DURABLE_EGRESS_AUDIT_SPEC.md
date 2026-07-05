# GATE F2 — durable, append-only LLM-egress audit — BUILD SPEC

- **Owning decision:** ADR 0105 (`docs/decisions/0105-durable-llm-egress-audit.md`), status PROPOSED.
- **Source:** ADR 0104 §Deferred — "F2 — durable, append-only egress audit (content-fingerprint +
  entity-manifest, moving off stdlib logging to a tamper-evident store)". Anchors verified against tree
  `db3b46b`.
- **Governance:** `human_fork: false`, `person_affecting: false`, `human_cosign: n/a` — non-person-affecting
  egress-audit substrate; the diff never touches `resolution/**`, `graph/writer.py`, or the
  person-affecting spine models. The one `db/models.py` touch is **additive-only** (a new model; every
  existing model byte-unchanged). See ADR 0105 §Person-affecting analysis.
- **Scope discipline:** F2 makes L1's *honest-today* egress audit **durable and tamper-evident**. It ships
  **DORMANT** behind a new default-off flag (`llm_egress_durable_enabled=False`), exactly the repo's
  dormant-substrate pattern (statement-spine dual-write ADR 0099, projector ADR 0100, rebuild-diff guard
  ADR 0102). The **enforced-classification egress gate (L2) is OUT** (DROPPED per F1). So are hash-chaining,
  HMAC fingerprints, content-derived manifests, a `seq` watermark, retention policy, and an export/verify
  CLI (§8, ADR 0105 §Deferred).
- **One slice, branched from `origin/master` as `gate/f2-durable-egress-audit`, individually mergeable**
  (SF-8: the table + writer + gateway wiring + migration are mutually dependent, dormant behind one flag).

The test-author writes RED tests first; the builder makes them GREEN without weakening any FROZEN
invariant. **Do NOT create the branch here** — the planner writes files only.

---

## 1. Verified current state (do not re-derive; confirm if editing)

| Fact | Location |
|---|---|
| The **only** egress audit today is stdlib logging via `egress_log.emit()` — ephemeral (rotation/crash loses it) | `llm/egress_log.py:56-105` |
| Pre-call stdlib emit (completeness, INV-S2-EGRESS) then post-call stdlib emit (usage, INV-USAGE) — L1's two-emit pattern | `llm/gateway.py:123-124`, `:151-152` |
| L1 fail-closed: external mode + `llm_egress_log_enabled=False` ⇒ `LLMGatewayError` before any emit/provider call | `llm/gateway.py:96-101` |
| `EgressRecord` MUTABLE dataclass; NEVER logs api_key or message content (ADR 0091 §3) | `llm/egress_log.py:21-40` |
| Gateway takes **only** `Settings` today — no DB access | `llm/gateway.py:59-64` |
| The gateway is constructed in `create_app`; `app.state.db_sessions` (a `sessionmaker`) is already built there | `api/main.py:117-129` |
| `get_llm_gateway` reads `app.state.llm_gateway` (ADR 0092 DI seam) — reused, unchanged | `api/deps.py:40-47` |
| `llm_egress_log_enabled: bool = True` (the L1 flag) | `settings.py:304` |
| Append-only spine idiom: INSERT-only `session.add` writers, caller-commits; model + migration byte-agreement (ADR 0030) | `resolution/statements.py`, `db/models.py:296-394` |
| **Known trap:** `StatementRecord.seq`/`DecisionRecord.seq` (ADR 0100) need a dialect-guarded `before_insert` SQLite listener because Postgres IDENTITY is a no-op on SQLite | `db/models.py:319-322`, `:428-448` |
| Latest migration is `0010_projection_outbox`; next is **`0011`** | `db/migrations/versions/` |
| Drift guard: `alembic check` + `_snapshot` equality of create_all vs alembic-head, per-table; the new table is exercised automatically | `tests/integration/test_migrations.py:78-170` |
| Canonicalization idiom already in-tree: `json.dumps(..., sort_keys=True)` (stdlib, no new dep); `hashlib` already used in connectors | `provenance/model.py:115,178`, `backup.py:204` |
| 15 existing `LLMGateway(settings)` construction sites (1 prod at `api/main.py:122`, the rest tests) | grep |

---

## 2. The slice — durable egress audit substrate

### 2.1 Files (allowed globs — this is `.claude/gate.scope`)

```
docs/decisions/0105-durable-llm-egress-audit.md          # this gate's ADR (already written)
docs/decisions/README.md                                 # regen ADR index (builder step 1)
docs/reviews/GATE_F2_DURABLE_EGRESS_AUDIT_SPEC.md         # this spec
src/worldmonitor/db/models.py                             # ADDITIVE: new LlmEgressRecord model only
src/worldmonitor/db/migrations/versions/0011_llm_egress_audit.py  # NEW migration (revises 0010)
src/worldmonitor/llm/egress_audit.py                     # NEW — fingerprint + row builders + writer
src/worldmonitor/llm/gateway.py                          # durable-write branches + session_factory seam + entity_ids kwarg
src/worldmonitor/settings.py                             # NEW field llm_egress_durable_enabled=False
src/worldmonitor/api/main.py                             # one-line: wire session_factory=db_sessions
tests/unit/test_llm_egress_audit.py                      # NEW — writer/fingerprint/row-builder unit cases
tests/property/test_prop_llm_egress_durable.py           # NEW — P-AUDIT-1..6 @given (spies/pure — no Docker)
tests/integration/test_llm_egress_durable.py             # NEW — real-Postgres round-trip + append-only at DB level
tests/unit/test_settings.py                              # UPDATE (additive): new-flag default/override cases
.claude/gate.scope
```

**Not in scope** (enforced by the freeze, §7): `resolution/**`, `graph/**`, `llm/modes.py`,
`llm/claude_shim.py`, `llm/egress_log.py`, `mcp/**`, `authz/**`, `api/llm.py`, `api/deps.py`,
`api/middleware.py`, `docs/fable-review/**` (a parallel consult doc lands there this session — keep it
out of gate scope), and **every existing `db/models.py` model** (the diff to that file is additive only).

### 2.2 The `llm_egress` table (`LlmEgressRecord` in `db/models.py`) — additive, append-only

Append-only, INSERT-only, `String(64)` UUID pk, **no `seq` IDENTITY** (SF-3 — no consumer; avoids the
ADR-0100 SQLite `before_insert` trap; **do NOT register a `before_insert` listener for this model**).
Two row kinds per crossing (`phase`), correlated by `call_id`. Model + migration `0011` byte-identical
(ADR 0030 drift guard).

| column | type | null | value at write time |
|--------|------|------|---------------------|
| `id` | `String(64)` PK | no | fresh `uuid4()` per ROW |
| `call_id` | `String(64)`, indexed | no | shared `uuid4()` correlating the attempt + completed rows of one crossing |
| `phase` | `String(16)`, indexed | no | `"attempt"` (pre-call) \| `"completed"` (post-call) |
| `mode` | `String(32)` | no | `EgressRecord.mode.value` |
| `confidentiality` | `Text` | no | `EgressRecord.confidentiality` — Text, NOT a bounded String: the registry labels are prose (CLAUDE_HEADLESS's is 153 chars); a bounded column truncation-refuses the fail-closed external write and bricks the mode (adversarial-verify HIGH, fixed) |
| `target_host` | `String(255)` | no | `EgressRecord.target_host` |
| `data_left_perimeter` | `Boolean` | no | `EgressRecord.data_left_perimeter` |
| `model` | `String(255)` | no | resolved model string |
| `caller_tag` | `Text` | no | `EgressRecord.caller_tag` (honest caller, L1-b) — Text: an unbounded JWT subject must never truncation-refuse the audit write |
| `content_fingerprint` | `String(64)` | **yes** | attempt rows: `sha256` hex of the canonical messages; NULL on completed rows |
| `entity_manifest` | `JSONB` | **yes** | attempt rows: caller-declared `list[str]` of canonical ids, or NULL (not declared / wire callers); NULL on completed rows |
| `prompt_tokens` | `Integer` | **yes** | completed rows: `usage.prompt_tokens`; NULL on attempt rows |
| `completion_tokens` | `Integer` | **yes** | completed rows: `usage.completion_tokens`; NULL on attempt rows |
| `total_tokens` | `Integer` | **yes** | completed rows: `usage.total_tokens`; NULL on attempt rows |
| `created_at` | `DateTime(timezone=True)`, `server_default now()` | no | insert time (ordering display; attempt < completed by construction) |

Honesty notes (mirror ADR 0099's discipline):
- **No `content` column, no `api_key` column — ever.** `content_fingerprint` is the durable, non-leaking
  stand-in (ADR 0091 §3, extended). Enforced by P-AUDIT-3.
- **`entity_manifest` is caller-declared, not content-derived** (SF-2). `/v1` wire traffic passes `None`
  → the column is NULL for wire callers. This is recorded honestly, not faked.
- **`content_fingerprint`/`entity_manifest` NULL on completed rows; token columns NULL on attempt rows.**
  Each row is self-describing via the repeated context columns (mode/target/caller/model/perimeter); the
  `phase`-specific columns split "what/whose" (attempt) from "how-much" (completed).
- Adds `Boolean`, `Integer` to the `db/models.py` sqlalchemy import line — an additive import; every
  existing model class stays **byte-unchanged** (including the `_assign_sqlite_seq` listener block).

Migration `0011_llm_egress_audit.py`: `revision = "0011_llm_egress_audit"`,
`down_revision = "0010_projection_outbox"`. `upgrade()` creates `llm_egress` + indexes on `call_id`,
`phase` (use `postgresql.JSONB()` for `entity_manifest`, `sa.Boolean()`, `sa.Integer()`). `downgrade()`
drops the indexes then the table. Do NOT edit `0001`–`0010` — history is immutable.

### 2.3 The durable writer module (`llm/egress_audit.py`, NEW)

Sibling to `egress_log.py`; keeps `egress_log.py` byte-unchanged (FROZEN). Module-level functions only
(NOT methods on `LLMGateway` — keeps `test_gateway_has_no_public_egress_bypass_other_than_chat` green).

```
def fingerprint_messages(messages: list[dict[str, Any]]) -> str:
    # Canonical-JSON (repo idiom) → sha256 → 64-char hex. Stdlib only; TOTAL — never raises
    # (adversarial-verify fix round): default=str for unserializable leaf VALUES; the digest
    # encodes via .encode("utf-8", "surrogatepass") (lone UTF-16 surrogates are WIRE-reachable
    # via stdlib json.loads escapes and must not raise UnicodeEncodeError); any remaining
    # serialization failure (mixed-type keys TypeError / circular ValueError / raising __str__)
    # falls back to a coarse deterministic type-level sentinel before hashing.
    #   try: canonical = json.dumps(messages, sort_keys=True, separators=(",", ":"),
    #                               ensure_ascii=False, default=str)
    #   except Exception: canonical = f"unserializable:{type(messages).__qualname__}:{len}"
    #   return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
    # Determinism domain, honest: byte-deterministic for JSON-shaped payloads (the /v1 wire
    # case); type-level only for non-serializable in-process payloads.

def build_attempt_row(call_id: str, record: EgressRecord, fingerprint: str,
                      entity_ids: list[str] | None) -> LlmEgressRecord:
    # phase="attempt"; content_fingerprint=fingerprint; entity_manifest=entity_ids (or None);
    # token columns None; fresh uuid4 id; context columns from `record`.

def build_completed_row(call_id: str, record: EgressRecord) -> LlmEgressRecord:
    # phase="completed"; content_fingerprint/entity_manifest None; token columns from record.usage
    # via getattr(...) (defensive, same as egress_log._extract_usage_tokens); fresh uuid4 id.

def write_row(session_factory: Callable[[], Session], row: LlmEgressRecord) -> None:
    # Owns its own short transaction (unlike statements.py where the caller commits): the pre-call
    # write must commit BEFORE the provider call.
    #   session = session_factory()
    #   try: session.add(row); session.commit()
    #   finally: session.close()
    # INSERT-only: never UPDATE, never DELETE, never session.delete. Propagates on commit failure
    # (the gateway decides fail-closed vs best-effort).
```

Append-only invariants (documented in the module docstring, mirroring `statements.py`):
- The writer only INSERTs (`session.add` + `commit`); no UPDATE, no DELETE, no `session.delete`.
- No column ever holds message content or the api key; the fingerprint is a fixed-length hex digest.

### 2.4 The gateway control flow (`gateway.py`) — the exact required flow inside `chat()`

Additive imports (all inside `llm/`, INV-CHOKE unaffected): `import uuid`, `import logging` +
`logger = logging.getLogger(__name__)`, `import worldmonitor.llm.egress_audit as egress_audit`.

Constructor: `def __init__(self, settings: Settings, session_factory: Callable[[], Session] | None = None)`
→ store `self._session_factory = session_factory`. Default `None` preserves all existing constructions.

New `chat()` kwarg: `entity_ids: list[str] | None = None` (additive; default `None` → all existing call
sites and `/v1` wire callers unaffected).

```
active_mode = mode if mode is not None else self._active_mode
mode_record  = REGISTRY[active_mode]
external     = mode_record.data_left_perimeter

# ── INV-FAILCLOSED (L1, ADR 0104 item 3) — UNCHANGED ─────────────────────────────────
if external and not self._settings.llm_egress_log_enabled:
    raise LLMGatewayError("external LLM egress refused: audit is disabled ...")   # existing text

durable_on = (self._settings.llm_egress_log_enabled
              and self._settings.llm_egress_durable_enabled)

# ── INV-DURABLE-FAILCLOSED (F2) — external + durable obligation + unwired sink ⇒ refuse ─
if external and durable_on and self._session_factory is None:
    raise LLMGatewayError(
        "external LLM egress refused: durable audit is enabled "
        "(llm_egress_durable_enabled=True) but no durable sink is wired — "
        "no durable audit, no egress"
    )

# ... claude-shim registration (UNCHANGED) ...
model, api_base, api_key = self._resolve_call_params(active_mode)
target_host = _extract_target_host(api_base, active_mode)

call_id = str(uuid.uuid4())
record  = EgressRecord(..., caller_tag=caller_tag, usage=None)   # unchanged fields

# stdlib PRE-call emit — UNCHANGED
if self._settings.llm_egress_log_enabled:
    egress_log.emit(record)

# ── F2 durable PRE-call ("attempt") row. The row BUILD (incl. the fingerprint) sits
# INSIDE the per-mode guarded blocks (adversarial-verify fix round): an audit-side failure
# follows the same policy as the write — external ⇒ typed LLMGatewayError fail-closed,
# LOCAL ⇒ best-effort warn. Previously the build sat outside and a hostile-payload
# serialization error escaped chat() as a raw untyped exception (HTTP 500 on /v1).
if durable_on and self._session_factory is not None:
    if external:
        # fail-closed: build + commit MUST succeed BEFORE the provider call
        try:
            attempt = egress_audit.build_attempt_row(
                call_id, record, egress_audit.fingerprint_messages(messages), entity_ids
            )
            egress_audit.write_row(self._session_factory, attempt)
        except Exception as exc:
            raise LLMGatewayError(
                "external LLM egress refused: durable audit write failed — no durable audit, no egress"
            ) from exc
    else:
        # LOCAL best-effort: an audit-side failure (build OR write) must not break a
        # confidential on-perimeter call
        try:
            attempt = egress_audit.build_attempt_row(
                call_id, record, egress_audit.fingerprint_messages(messages), entity_ids
            )
            egress_audit.write_row(self._session_factory, attempt)
        except Exception:
            logger.warning("durable egress audit write failed (LOCAL, best-effort)", exc_info=True)

# provider call — UNCHANGED (whole-module litellm.completion so tests can monkeypatch)
try:
    response = litellm.completion(model, messages, **call_kwargs)
except Exception as exc:
    raise LLMGatewayError(...) from exc          # UNCHANGED

usage_val = getattr(response, "usage", None)
if usage_val is not None:
    record.usage = usage_val

# stdlib POST-call emit — UNCHANGED
if self._settings.llm_egress_log_enabled:
    egress_log.emit(record)

# ── F2 durable POST-call ("completed") row — best-effort for BOTH modes ──────────────
if durable_on and self._session_factory is not None:
    try:
        egress_audit.write_row(self._session_factory, egress_audit.build_completed_row(call_id, record))
    except Exception:
        logger.warning("durable egress usage row write failed (best-effort; crossing already audited)",
                       exc_info=True)

return response
```

Notes the builder MUST honour:
- **Dormant default:** `llm_egress_durable_enabled=False` ⇒ `durable_on=False` ⇒ no durable rows and no
  new refuse condition ⇒ behaviour is byte-identical to L1 (the FROZEN completeness property stays green).
- The stdlib pre/post emits and the L1 fail-closed check are **UNCHANGED** — only additive durable
  branches are inserted around them.
- **Provider failure after the attempt row ⇒ exactly one durable row** (no completed row).
- The fingerprint is computed from **`messages`** (the outbound payload). Never store `messages` itself.

### 2.5 Settings (`settings.py`) — one additive field

```
# Durable, append-only LLM-egress audit (ADR 0105 / F2). When True (and the master
# llm_egress_log_enabled is also True), each crossing writes a durable Postgres row; an
# EXTERNAL crossing is fail-closed on that write (DB down / sink unwired ⇒ refuse). Default
# False: DORMANT — behaviour is byte-identical to L1 until an operator enables it after
# applying migration 0011 and confirming the Postgres sink.
llm_egress_durable_enabled: bool = False
```

Additive; `test_settings.py` gets two small cases (default False; override True). No exact-schema
assertion exists to break (verified: `test_settings.py` is per-field).

### 2.6 Wiring (`api/main.py`) — one line

`if llm_gateway is None: llm_gateway = LLMGateway(settings)` →
`LLMGateway(settings, session_factory=db_sessions)`. Reuses the `sessionmaker` already built at
`api/main.py:117-118` (the same one the review-queue UI reads Postgres through, ADR 0069/0103). Dormant
default flag ⇒ the factory is unused until the operator enables durable auditing. `get_llm_gateway`
(`api/deps.py`) is **unchanged**.

---

## 3. Property invariants (@given — RED-first)

For each: **NAME · STATEMENT · GENERATOR · ORACLE · NON-VACUITY.** Property tests live in
`tests/property/test_prop_llm_egress_durable.py` and **must not need Docker** — use a **spy
session/factory** (records `add`/`commit`/`close`/`delete`/`execute` calls; can be told to raise on
commit) and **pure-function** checks. Do NOT round-trip through real SQLite DDL (the `entity_manifest`
JSONB column is Postgres-shaped; real-DB fidelity is the integration test, §4). Build settings with a
helper mirroring `_make_test_settings(llm_mode=…, llm_egress_log_enabled=True,
llm_egress_durable_enabled=True)`; patch `litellm.completion`. **Heed the known trap** (memory:
given-red-tests-leak-connections): if any example opens a resource, dispose in `try/finally`.

**P-AUDIT-1 — external-call completeness.**
- *Statement:* with `durable_on=True`, any **external** `chat()` attempt writes+commits a durable
  **attempt** row **before** `litellm.completion` is contacted — even when the provider raises.
- *Generator:* `@given` over external `mode ∈ {OPENROUTER, CLAUDE_HEADLESS}` × `provider_raises ∈
  {True, False}` × generated `messages`.
- *Oracle:* an ordered event list records `"durable_commit"` (spy session commit) and `"provider"`
  (litellm spy); assert `"durable_commit" ∈ events` and it precedes `"provider"`; on `provider_raises`
  the `LLMGatewayError` surfaces yet the committed attempt row still exists (spy captured ≥1 row with
  `phase=="attempt"`).
- *Non-vacuity:* an impl that writes after the provider call fails the ordering / raises-case; today's
  no-durable code fails "durable_commit ∈ events".

**P-AUDIT-2 — append-only (no UPDATE/DELETE).**
- *Statement:* across an arbitrary sequence of `chat()` calls (mixed modes, mixed success), the writer
  issues only INSERTs — never an UPDATE, never a DELETE, never `session.delete`.
- *Generator:* `@given` over `lists(fixed_dictionaries({mode, provider_raises}), 1..5)`.
- *Oracle:* the spy session records every method call; assert the recorded call-name set ⊆
  `{add, commit, close, flush}` and that `delete` was never called and no `execute` carried an
  UPDATE/DELETE statement.
- *Non-vacuity:* an impl that "enriches" the attempt row in place (an UPDATE) fails; a `session.delete`
  cleanup fails.

**P-AUDIT-3 — no-content-leak.**
- *Statement:* for arbitrary message payloads (incl. an embedded api-key-looking secret) and an
  arbitrary declared manifest, **no** serialized column of either built row contains the raw message
  text or the api key; `content_fingerprint` is a 64-char lowercase hex string.
- *Generator:* `@given` over generated `messages` (with a sentinel secret substring) × `entity_ids`
  (or None) × a sentinel api key in settings.
- *Oracle:* build both rows via `build_attempt_row` / `build_completed_row`; for every column value
  `str(v)` assert the sentinel message text and the api key are absent; assert
  `re.fullmatch(r"[0-9a-f]{64}", row.content_fingerprint)`.
- *Non-vacuity:* a row that stored `messages` (or a preview) fails; a truncated-but-present content
  column fails.

**P-AUDIT-4 — fingerprint determinism + sensitivity.**
- *Statement:* `fingerprint_messages` is deterministic (equal canonical messages ⇒ equal digest, incl.
  key-order-insensitive dicts) and sensitive (any content change ⇒ different digest); output is always
  64-hex; it never raises on hostile content.
- *Generator:* `@given` over generated `messages` and a mutation (change/add/remove a value); also a
  key-reordered copy of the same dict.
- *Oracle:* `f(m) == f(reorder(m))`; `f(m) != f(mutate(m))` (unless the mutation is a no-op — guard the
  strategy so the mutation is real); `len(f(m)) == 64` and hex.
- *Non-vacuity:* a constant/`repr()`-based or key-order-sensitive digest fails; a digest that ignores
  the mutated field fails.

**P-AUDIT-5 — durable fail-closed asymmetry.**
- *Statement:* with `durable_on=True` and the durable **write raising**: an **external** crossing raises
  `LLMGatewayError` and `litellm.completion` is **never** called; a **LOCAL** crossing proceeds (provider
  **is** called, best-effort). Also: external + `session_factory is None` ⇒ `LLMGatewayError`, provider
  never called.
- *Generator:* `@given` over `mode ∈ LLMMode` × `sink_state ∈ {raises, none, ok}`.
- *Oracle:* a `litellm.completion` spy (call count) + a spy factory. For `(external, raises)` /
  `(external, none)`: `pytest.raises(LLMGatewayError)`, provider count == 0. For `(LOCAL, raises)`,
  `(any, ok)`: no raise, provider count == 1.
- *Non-vacuity:* an always-raise impl fails the LOCAL/ok cases; a never-raise impl (today) fails the
  external-raises/none cases.

**P-AUDIT-6 — two-row usage correlation.**
- *Statement:* a **successful** call (durable_on, response carries `usage`) writes exactly two durable
  rows sharing one `call_id`: an `"attempt"` row (fingerprint set, tokens NULL) then a `"completed"` row
  (tokens = response counts, fingerprint NULL); a provider failure after the attempt row leaves exactly
  one row.
- *Generator:* `@given` over `mode ∈ LLMMode` (durable enabled) × generated `(prompt, completion, total)`
  token integers × `provider_raises ∈ {True, False}`.
- *Oracle:* the spy captures rows in order; on success assert 2 rows, same `call_id`, `phase` order
  `["attempt", "completed"]`, attempt `content_fingerprint` is hex + tokens None, completed tokens match
  the generated counts + `content_fingerprint` None; on failure assert 1 row (`phase=="attempt"`).
- *Non-vacuity:* a single-row impl fails the success case; an in-place-update impl fails the two-row +
  append-only (P-AUDIT-2) checks; a mismatched `call_id` fails correlation.

**P-DORMANT (metamorphic, may live in the unit file) — dormant default is inert.**
- *Statement:* with `llm_egress_durable_enabled=False` (default), across all modes and success/failure,
  the durable writer is **never** invoked (spy factory never called) and the L1 behaviour (stdlib emits,
  fail-closed-on-`llm_egress_log_enabled`) is unchanged.
- *Non-vacuity:* an impl that writes durably regardless of the flag fails.

---

## 4. Unit + integration tests

**`tests/unit/test_llm_egress_audit.py` (NEW):** `fingerprint_messages` golden cases (known input →
stable digest; empty messages; unicode; hostile non-serializable value does not raise);
`build_attempt_row` / `build_completed_row` column mapping (phase, fingerprint/manifest vs tokens,
`call_id` carried); `write_row` calls `add`+`commit`+`close` and propagates a commit error.

**`tests/integration/test_llm_egress_durable.py` (NEW, `pytest.mark.integration`, real Postgres —
Docker IS available locally, run it):** apply migrations to a fresh DB; drive a real `chat()` with
`durable_on=True` + an injected testcontainer `sessionmaker` + a patched `litellm.completion`; SELECT
the rows and assert (a) two rows on success with a shared `call_id`, correct `phase`, hex fingerprint,
tokens on the completed row; (b) column fidelity (lengths, JSONB manifest round-trips, NULLs where
specified); (c) **no** column contains the message text / api key; (d) an external crossing with the DB
made unreachable refuses (`LLMGatewayError`) and writes nothing. `tests/integration/test_migrations.py`
stays green **unchanged** — its `reference_schema`/drift guard exercises the new `llm_egress` table
automatically once model + migration agree.

---

## 5. Builder task list (ordered)

1. Regenerate the ADR index: `uv run python scripts/gen_adr_index.py` (adds the `0105` row), then
   `uv run python scripts/gen_adr_index.py --check` passes.
2. `db/models.py`: add `Boolean`, `Integer` to the sqlalchemy import; append the `LlmEgressRecord`
   model (§2.2). **No `seq`, no `before_insert` listener.** Do NOT edit any existing model.
3. `db/migrations/versions/0011_llm_egress_audit.py`: new migration (§2.2), byte-agreeing with the model.
4. `llm/egress_audit.py`: `fingerprint_messages`, `build_attempt_row`, `build_completed_row`,
   `write_row` (§2.3).
5. `settings.py`: add `llm_egress_durable_enabled: bool = False` (§2.5).
6. `llm/gateway.py`: `session_factory` ctor param + `entity_ids` kwarg + the durable-write branches
   (§2.4). Additive imports only; INV-CHOKE untouched.
7. `api/main.py`: wire `session_factory=db_sessions` (§2.6).
8. `test_settings.py`: additive default/override cases for the new flag.
9. Make the RED tests GREEN (P-AUDIT-1..6, P-DORMANT, unit, integration).

---

## 6. Acceptance criteria (all measurable)

- **FULL** `uv run pytest -m "not integration"` GREEN (repo-wide) — the `quality` job runs exactly this.
- **Integration GREEN locally:** `uv run pytest -m integration` (Docker available) — the new
  `test_llm_egress_durable.py` **and** the unchanged `test_migrations.py` drift guard pass.
- All new `@given` properties GREEN: P-AUDIT-1..6, P-DORMANT.
- **FROZEN-adjacent tests stay green unchanged:** `tests/property/test_llm_egress_completeness.py`
  **byte-unchanged**; `tests/unit/test_llm_gateway.py` and `tests/property/test_prop_llm_egress_hardening.py`
  green **unchanged** (dormant default flag + `session_factory=None` default preserve every existing
  construction and assertion).
- `ruff format --check .` (REPO-WIDE) clean; `ruff check .` clean; `uv run pyright` clean.
- `uv run python scripts/gen_adr_index.py --check` passes with the `0105` row (`PROPOSED | 2026-07-05 |
  false | false`).
- `quality` + `security` (+ `adr-index` where required) CI checks green before merge; `gh pr checks
  <N> --watch` before any merge.
- ADR 0105 `human_cosign: n/a` stays as written; **no** person-affecting cosign is added (the diff never
  touches `resolution/**`; the `db/models.py` touch is additive-only). Status flips PROPOSED → ACCEPTED
  at merge after judge APPROVE.

---

## 7. Invariants the checker MUST reproduce

- **INV-DURABLE-COMPLETE** — durable_on + external ⇒ an `"attempt"` row is committed BEFORE
  `litellm.completion`; a provider raise still leaves that committed row. (P-AUDIT-1)
- **INV-DURABLE-APPENDONLY** — the writer issues only INSERTs; never UPDATE, never DELETE, never
  `session.delete`, across arbitrary call sequences. (P-AUDIT-2)
- **INV-DURABLE-NOLEAK** — no durable row column ever holds message content or the api key;
  `content_fingerprint` is a 64-char hex `sha256`; `entity_manifest` is caller-declared ids only.
  (P-AUDIT-3)
- **INV-FINGERPRINT** — `fingerprint_messages` is deterministic (key-order-insensitive) + sensitive +
  64-hex + never raises. (P-AUDIT-4)
- **INV-DURABLE-FAILCLOSED** — durable_on + external + (write raises OR unwired factory) ⇒
  `LLMGatewayError`, provider never contacted; LOCAL + write raises ⇒ provider still contacted
  (best-effort). (P-AUDIT-5)
- **INV-DURABLE-USAGE** — success ⇒ two rows sharing `call_id` (`attempt` then `completed`, tokens on
  the completed row); provider failure after the attempt ⇒ exactly one row. (P-AUDIT-6)
- **INV-DORMANT** — `llm_egress_durable_enabled=False` (default) ⇒ no durable write, no new refuse; L1
  behaviour byte-identical. (P-DORMANT)
- **INV-L1-PRESERVED** — INV-CHOKE (no external-SDK import outside `llm/`), INV-FAILCLOSED (external +
  `llm_egress_log_enabled=False` ⇒ refuse), INV-USAGE (two stdlib emits on success), INV-S2-EGRESS
  (write-before-call) all remain green; `egress_log.emit` behaviour is byte-unchanged.
- **INV-MODEL-ADDITIVE** — the `db/models.py` diff adds only `LlmEgressRecord` (+ two imports); every
  existing model + the `_assign_sqlite_seq` listener block is byte-unchanged; the migration drift guard
  proves the existing tables' schemas are unchanged.

---

## 8. FROZEN (byte-unchanged — the checker verifies `git diff` touches none of these)

- The entire **`resolution/**`** (clustering, thresholds, `signoff.py`, `guard.py`/sensitivity,
  `gold.py`, `eval.py`, `pipeline.py`, `statements.py`, `projector.py`, `divergence.py`,
  merge/referents/canonical) and **`graph/**`** (incl. `graph/writer.py`). F2 has no reason to touch the
  person-affecting write path.
- **Every existing `db/models.py` model** — `ConnectorInstance`, `ErQueueItem`, `MergeAudit`,
  `IngestDeadLetter`, `MergeAlert`, `TaskRun`, `ResolverJudgement`, `SignOff`, `CanonicalIdLedger`,
  `ErGoldPair`, `StatementRecord`, `DecisionRecord`, `ProjectionCheckpoint`, and the `_assign_sqlite_seq`
  `before_insert` block. The diff to this file is **additive only** (one new model + two imports).
- **Existing migrations `0001`–`0010`** — history is immutable; the delta lives only in `0011`.
- **`llm/egress_log.py`** — the stdlib `emit()` / `EgressRecord` (L1 / INV-S2-EGRESS). The durable table
  sits ALONGSIDE it, unchanged.
- **`llm/modes.py`** — the ADR-0091 locked three-mode `REGISTRY` (per-mode `data_left_perimeter` /
  confidentiality / badge). F2 only *reads* `mode_record.data_left_perimeter`.
- **`llm/claude_shim.py`** — the CLAUDE_HEADLESS shim.
- **`mcp/**`, `authz/**`, `api/llm.py`, `api/deps.py`, `api/middleware.py`** — F2 wires only the gateway
  construction (`api/main.py`); the `/v1` handler, the role gate, and `get_llm_gateway` are unchanged
  (wire callers get `entity_ids=None` via the kwarg default).
- **`tests/property/test_llm_egress_completeness.py`** — stays green **unchanged** (dormant default +
  `session_factory=None` default). If the test-author believes it must change, that is a red flag — STOP
  and escalate.
- **`tests/integration/test_migrations.py`** — unchanged; its drift guard exercises the new table.
- **`docs/fable-review/**`** — a parallel consult doc lands there this session; keep it out of gate scope.

---

## 9. OUT OF SCOPE (do NOT build here — ADR 0105 §Deferred)

- **Hash-chain tamper-evidence** (prev-row digest) — SF-6 revisit; needs concurrent-append serialization.
- **HMAC-keyed fingerprint** — SF-1 revisit (additive, forward-only column).
- **Content-derived entity manifest** — SF-2 revisit; must not reintroduce L2 classification.
- **A `seq` monotonic watermark** — SF-3 revisit; only if a real egress-log consumer appears.
- **Retention / rotation policy for `llm_egress`** — a later gate (the table grows unbounded by design).
- **An export / verification CLI** — a later gate.
- **L2 — enforced-classification egress gate** — DROPPED per F1 (ADR 0104). F2 audits; it does not
  classify or block by data classification.
- Any change to ER/thresholds/merge/guard/gold/scores/erasure/statements/migrations `0001`–`0010`, the
  ADR-0091 three-mode registry, the `/v1` role gate, or streaming SSE.
