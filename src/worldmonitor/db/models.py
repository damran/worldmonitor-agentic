"""SQLAlchemy 2.0 models — the relational side of the platform.

Phase 1 needs two tables: the connector-instance registry that the Integrations
page reads/writes (config stored **encrypted**, see
:mod:`worldmonitor.db.crypto`), and the entity-resolution queue connectors push
mapped candidates onto. The platform is single-tenant (D1, ADR 0042).
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class ConnectorInstance(Base):
    """A configured instance of a connector plugin."""

    __tablename__ = "connector_instance"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(128), index=True)
    # Fernet-encrypted JSON config blob — never stored in plaintext.
    config_encrypted: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String(32), default="disabled")
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    next_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # G8 resume cursor (ADR 0070): the saved source position for a STREAM connector. NULL for every
    # batch connector (additive, backward-compatible); the driver injects it into the run config's
    # ``_cursor`` before a run and persists ``IngestStats.last_cursor`` back onto it after.
    stream_cursor: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ErQueueItem(Base):
    """A mapped FtM candidate awaiting entity resolution (L3 owns canonicalization)."""

    __tablename__ = "er_queue_item"
    # Idempotent enqueue (ADR 0029 / A6): the same landing record + FtM entity id
    # enqueues at most once, so a re-ingest after a crash/restart does not
    # double-enqueue. ``entity_id`` is NULL for an id-less entity; Postgres
    # treats NULLs as distinct, so those rare rows are not deduped.
    __table_args__ = (UniqueConstraint("source_record", "entity_id", name="uq_er_queue_dedup"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(128))
    # The FtM entity id (``raw_entity["id"]``) — part of the dedup key; NULL if absent.
    entity_id: Mapped[str | None] = mapped_column(String(255), index=True, default=None)
    # The mapped FtM entity (with provenance in its context), pending resolution.
    raw_entity: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Pointer to the verbatim raw record in the landing zone.
    source_record: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MergeAudit(Base):
    """Audit trail of every resolution decision — which sources collapsed and why.

    Doubles as the rollback record (CLAUDE.md: merge audit trail; never silent
    in-place mutation). ``decision`` is ``merged`` or ``pending_review``.
    """

    __tablename__ = "merge_audit"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(255), index=True)
    source_ids: Mapped[list[str]] = mapped_column(JSONB)
    score: Mapped[float]
    decision: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestDeadLetter(Base):
    """A record quarantined during ingest OR resolution (the dead-letter trail).

    No single bad record may abort a run (audit gaps G8, B-2): the failure is recorded
    here and processing continues. ``stage`` is one of: ``"land"`` (raw bytes could not be
    written to the landing zone — ``source_record`` is null) / ``"map"`` (the raw landed but
    mapping to FtM raised — ``source_record`` points at the landed bytes); or, from
    resolution (ADR 0038), ``"resolve-row"`` (a row ``make_entity`` could not parse),
    ``"resolve-batch"`` (a window that could not be scored/clustered), ``"resolve-write"``
    (a merged canonical that failed FtM validation), or ``"resolve-noid"`` (an unclustered /
    id-less row); or, from H-2 / sign-off integrity (ADR 0041),
    ``"resolve-incompat"`` (a schema-incompatible member dropped from a transitive cluster and
    re-emitted as its own singleton — a NON-status-mutating skip audit, the row still resolves)
    or ``"signoff-poison"`` (a malformed ``raw_entity`` skipped during a sign-off queue scan so
    one poison row cannot wedge approve/reject). ``error`` is a bounded exception
    summary. All quarantines are replayable.
    """

    __tablename__ = "ingest_dead_letter"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(128))
    # The record's source key (the would-be landing key); always known.
    source_key: Mapped[str] = mapped_column(String)
    # s3:// pointer to the landed bytes; null for a land-stage failure (nothing landed).
    source_record: Mapped[str | None] = mapped_column(String, default=None)
    stage: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MergeAlert(Base):
    """Durable, auditable record of a flagged cluster that was merged anyway.

    Written only under ``MERGE_GUARD_MODE="alert"`` (build phase, ADR 0024): the
    catastrophic-merge guard still flags oversized / PEP / sanctioned clusters,
    but instead of parking them in ``pending_review`` the pipeline writes the
    merge and records it here. This is the trail a human reviews before flipping
    the guard back to ``"block"`` with sign-off (CLAUDE.md self-improvement rule).
    ``reason`` is the guard's sensitivity reason (oversized / PEP / sanctioned).
    """

    __tablename__ = "merge_alerts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(255), index=True)
    source_ids: Mapped[list[str]] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(String, default="")
    score: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskRun(Base):
    """One driver-initiated unit of work — the run-history + observability trail (ADR 0029).

    The long-running ingest driver records a row per ``kind`` (``"ingest"`` for a
    connector run, ``"resolve"`` for a resolution pass): ``status`` moves
    ``"running"`` → ``"ok"`` / ``"error"``, ``stats`` holds the run's counts
    (Ingest/ResolveStats), and ``error`` carries a bounded summary on failure. A row
    left ``"running"`` after a crash is reset to ``"error"`` on driver startup
    (single-node assumption; the deferred lease replaces it under HA).

    ACTIVE-capability gating audit (ADR 0071): ``run_mode`` distinguishes a cadence-driven run
    (default ``"cadence"``) from an operator-triggered one (``"operator"``); ``triggered_by`` is the
    authenticated operator subject; ``scope_token`` is the minted, tamper-evident per-run
    authorization (the audit proof of *what was authorized, by whom*). For a cadence run the latter
    two are ``NULL`` and ``run_mode`` stays ``"cadence"`` — purely additive, behaviour-preserving.
    """

    __tablename__ = "task_run"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connector_instance_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    error: Mapped[str] = mapped_column(String, default="")
    # ACTIVE-capability gating audit (ADR 0071) — additive; cadence runs keep the defaults.
    run_mode: Mapped[str] = mapped_column(String(16), default="cadence")
    triggered_by: Mapped[str | None] = mapped_column(String(255), default=None)
    scope_token: Mapped[str | None] = mapped_column(String, default=None)


class ResolverJudgement(Base):
    """A DURABLE resolver judgement that survives the per-batch resolver.

    The per-batch resolver is ephemeral (ADR 0028, ADR 0026 batch purity), so a
    judgement made in one batch evaporates. A human sign-off (ADR 0031) must persist:
    a REJECT writes a ``"negative"`` judgement so the records never re-merge; an
    APPROVE writes a ``"positive"`` one so they always do. EVERY batch's fresh resolver
    loads these judgements before clustering and they take precedence over Splink — so
    a reviewed cluster never re-parks. ``(left_id, right_id)`` is unique with
    ``left_id <= right_id`` (the pair is stored canonically ordered).
    """

    __tablename__ = "resolver_judgement"
    __table_args__ = (UniqueConstraint("left_id", "right_id", name="uq_resolver_judgement_pair"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    left_id: Mapped[str] = mapped_column(String(255))
    right_id: Mapped[str] = mapped_column(String(255))
    judgement: Mapped[str] = mapped_column(String(16))  # "positive" | "negative"
    source: Mapped[str] = mapped_column(String(32), default="signoff")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SignOff(Base):
    """A human decision on a parked (sensitive/oversized) merge (ADR 0031).

    Under ``MERGE_GUARD_MODE="block"`` the catastrophic-merge guard parks a flagged
    cluster as ``pending_review`` (never written). An operator reviews it and either
    APPROVES (the merge is promoted to the graph) or REJECTS (the members are written
    as separate entities). This is the durable, auditable sign-off trail CLAUDE.md
    requires for changes affecting a real person — ``approver`` is the operator
    identity (a string in v0; Zitadel-backed in Phase 2).
    """

    __tablename__ = "sign_off"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(255), index=True)
    source_ids: Mapped[list[str]] = mapped_column(JSONB)
    decision: Mapped[str] = mapped_column(String(16), index=True)  # "approved" | "rejected"
    approver: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CanonicalIdLedger(Base):
    """Durable canonical-id ledger — the anchor-preferred stable-id mapping (ADR 0044).

    Separates the two id concepts ADR 0036 conflated: ``wmc-<hash>`` stays *strictly* a
    crash-retry idempotency fingerprint (the fallback id of an *unanchored* merge), while
    DURABLE identity lives here, anchor-preferred (``qid:``/``lei:``/``regno:``/``taxno:``) or
    minted (``wm-mint-<uuid>``) and stable across re-ingest. A connector mints fresh per-collect
    member ids on every re-ingest (the very thing that churns the ``wmc-`` fingerprint); the
    durable id derived from the entity's anchor survives that churn.

    Two row kinds share the table (a self-row vs an alias row, distinguished by whether
    ``canonical_alias == canonical_id``):

    * a **canonical** row records a durable id with its anchor kind/value (``record_canonical``);
      it stores ``canonical_alias = canonical_id`` (the durable id is its own alias) — idempotent.
    * an **alias** row maps a superseded/prior id (a collapsed merge member, the prior ``wmc-``
      fingerprint, or a split-ejected id) to the surviving durable ``canonical_id`` — APPEND-ONLY:
      a split ADDS an alias row, never deletes (the no-un-merge invariant).

    ``(canonical_id, canonical_alias)`` is unique so ``record_alias`` and the canonical self-row
    are both idempotent (a duplicate (canonical, alias) is a no-op). The platform is single-tenant
    (D1, ADR 0042). This migration head and the ``0006_canonical_ledger`` migration MUST agree
    byte-for-byte (``tests/integration/test_migrations.py`` drift guard, ADR 0030).
    """

    __tablename__ = "canonical_id_ledger"
    __table_args__ = (
        UniqueConstraint("canonical_id", "canonical_alias", name="uq_canonical_id_ledger_alias"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The durable (anchor-preferred or minted) canonical id; indexed for the adopt/resolve reads.
    canonical_id: Mapped[str] = mapped_column(String(255), index=True)
    # A superseded/prior id that resolves to ``canonical_id`` (one append-only row per alias);
    # equals ``canonical_id`` for the canonical self-row. Indexed for ``resolve_durable``.
    canonical_alias: Mapped[str] = mapped_column(String(255), index=True)
    # The anchor kind that produced the durable id ("qid" | "lei" | "regno" | "taxno" | "mint").
    anchor_kind: Mapped[str] = mapped_column(String(16), default="")
    # The bare anchor value (e.g. the QID/LEI), or "" for a mint / an alias row.
    anchor_value: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ErGoldPair(Base):
    """A labelled gold record-pair for the ER measurement harness (ADR 0043 / Gate A).

    A small, seeded, reproducible set of ``match`` / ``non_match`` record pairs that the
    evaluation harness (:mod:`worldmonitor.resolution.eval`) scores the resolver against. It
    is the regression instrument every later ER gate measures over — it never mutates the
    graph (the harness READS, never writes, per the gate's locked invariants); these rows are
    id references plus a clerical label, not graph entities. Built by stratified uncertainty
    sampling over the 0.5-0.95 Splink-score band plus a seeded OS-Pairs-style hard-case set
    (:mod:`worldmonitor.resolution.gold`).

    The pair is stored **canonically ordered** (``left_id <= right_id``) and is unique on
    ``(left_id, right_id)`` — the same idiom as :class:`ResolverJudgement`'s
    ``uq_resolver_judgement_pair``. ``clerical_score`` maps to Splink's ``clerical_match_score``
    in the labels table; it is nullable (a deterministic hard case may carry no Splink score).
    """

    __tablename__ = "er_gold_pair"
    __table_args__ = (UniqueConstraint("left_id", "right_id", name="uq_er_gold_pair"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    left_id: Mapped[str] = mapped_column(String(255))
    right_id: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(16))  # "match" | "non_match"
    source: Mapped[str] = mapped_column(String(32))  # e.g. "uncertainty" | "os_pairs"
    clerical_score: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StatementRecord(Base):
    """Append-only per-claim log — the statement spine (Gate 2a / ADR 0099).

    One row per ``(subject, entity_id, schema, prop, value, dataset)`` claim the fused
    ``StatementEntity`` yields at merge time (the ``id`` pseudo-statement excluded).  Realises
    **G1 provenance at the per-claim grain**: ``dataset`` = ``Provenance.source_id``, plus the
    full G1 quad (``retrieved_at``, ``reliability``, ``raw_pointer``) on every claim.

    Append-only: the writers only INSERT (``session.add``); no UPDATE or DELETE. The
    ``supersedes`` / ``superseded_by`` write path and any ``statement_id`` uniqueness /
    idempotency constraint are Gate 3 concerns (ADR 0099 §Deferred).

    ``scope`` is a reserved forward-compatibility column (ADR 0099 Decision A): it is
    **UNENFORCED** — no code reads, writes (beyond the server default), or filters on it. It
    is NOT re-adding tenant scoping (ADR 0042, single-tenant D1 unchanged).

    This model and migration ``0009_statement_spine`` MUST agree byte-for-byte
    (``tests/integration/test_migrations.py`` drift guard, ADR 0030).
    """

    __tablename__ = "statement"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Monotonic server-assigned ordering column — the outbox watermark (ADR 0100 D1).
    # Server-assigned via Postgres IDENTITY; NEVER set by application code; append-only, additive.
    # Model + migration MUST agree byte-for-byte (test_migrations.py drift guard, ADR 0030).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), index=True, nullable=False)
    # FtM Statement.id — the deterministic content hash; dedup / backfill key (non-unique in step 1)
    statement_id: Mapped[str] = mapped_column(String(64), index=True)
    # SUBJECT = the cluster's durable canonical id (post-rekey)
    canonical_id: Mapped[str] = mapped_column(String(255), index=True)
    # The contributing source member id
    entity_id: Mapped[str] = mapped_column(String(255), index=True)
    schema: Mapped[str] = mapped_column(String(64))
    prop: Mapped[str] = mapped_column(String(64))  # never "id" — excluded on write
    # Unbounded TEXT — hostile-data rule: no cast on free-form connector values
    value: Mapped[str] = mapped_column(Text)
    # The member's Provenance.source_id (G1 source)
    dataset: Mapped[str] = mapped_column(String(255))
    # G1 quad — nullable when the member was genuinely unstamped (no invented provenance)
    reliability: Mapped[str | None] = mapped_column(String(16), default=None)
    # ISO-8601 string — Provenance.retrieved_at; stored as string, no cast (hostile-data rule)
    retrieved_at: Mapped[str | None] = mapped_column(String(64), default=None)
    # Provenance.source_record (landing-zone pointer); unbounded TEXT; NULL if unstamped
    raw_pointer: Mapped[str | None] = mapped_column(Text, default=None)
    first_seen: Mapped[str | None] = mapped_column(String(64), default=None)
    last_seen: Mapped[str | None] = mapped_column(String(64), default=None)
    # Unmodelled in step 1 — always NULL until a method field exists (ADR 0099 §table)
    method: Mapped[str | None] = mapped_column(String(64), default=None)
    # Reserved forward-compat (ADR 0099 Decision A) — UNENFORCED; single-tenant D1 unchanged
    scope: Mapped[str | None] = mapped_column(String(64), server_default=text("'default'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DecisionRecord(Base):
    """Append-only merge / split / negative belief-revision log (Gate 2a / ADR 0099).

    One row per **promoted merge** (``cluster.is_merge``, i.e. ≥2 source members). Step 1
    only ever writes ``kind="merge"``.  Distinct from the legacy ``merge_audit`` (which also
    records singletons + ``pending_review``) and from ``resolver_judgement`` / ``sign_off``
    (human pair sign-offs seeded into the resolver); the three coexist in step 1 and a later
    gate reconciles them.

    Append-only: the writers only INSERT; ``supersedes`` / ``superseded_by`` stay ``NULL`` in
    step 1 (the un-merge / belief-revision back-pointer is a Gate 3 concern).

    ``scope`` is a reserved forward-compatibility column (ADR 0099 Decision A): UNENFORCED —
    no code reads, writes (beyond the server default), or filters on it.  NOT re-adding tenant
    scoping (ADR 0042, single-tenant D1 unchanged).

    This model and migration ``0009_statement_spine`` MUST agree byte-for-byte
    (``tests/integration/test_migrations.py`` drift guard, ADR 0030).
    """

    __tablename__ = "decision"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Monotonic server-assigned ordering column — the outbox watermark (ADR 0100 D1).
    # Server-assigned via Postgres IDENTITY; NEVER set by application code; append-only, additive.
    # Model + migration MUST agree byte-for-byte (test_migrations.py drift guard, ADR 0030).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), index=True, nullable=False)
    # The id the decision acts on = cluster.canonical_id
    canonical_id: Mapped[str] = mapped_column(String(255), index=True)
    # "merge" only in step 1; "split" / "negative" reserved for later gates
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # list(cluster.member_ids) — the collapsed source ids (evidence of what merged)
    member_ids: Mapped[list[str]] = mapped_column(JSONB)
    # cluster.score — weakest-link match probability
    score: Mapped[float] = mapped_column(Float)
    # "auto:resolver" — the automated decider; human-decision path is a later gate
    decided_by: Mapped[str] = mapped_column(String(255))
    # {"reason": reason} when the guard reason is non-empty, else NULL
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # Belief-revision back-pointers — always NULL in step 1 (reserved for Gate 3 un-merge)
    supersedes: Mapped[str | None] = mapped_column(String(64), default=None)
    superseded_by: Mapped[str | None] = mapped_column(String(64), default=None)
    # Reserved forward-compat (ADR 0099 Decision A) — UNENFORCED; single-tenant D1 unchanged
    scope: Mapped[str | None] = mapped_column(String(64), server_default=text("'default'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectionCheckpoint(Base):
    """Per-target projection watermark — the fold engine's resumable checkpoint (ADR 0100 D1).

    One row per projection target (e.g. ``id="neo4j"``). The projector reads rows past the
    watermark (``seq > last_statement_seq / last_decision_seq``), folds them, writes Neo4j
    FIRST (idempotent MERGE), then advances the watermark and commits — at-least-once ordering.
    A crash between the two replays the delta on restart (idempotent).

    Append-only-friendly, additive, single-tenant D1 unchanged (ADR 0042). Model + migration
    ``0010_projection_outbox`` MUST agree byte-for-byte (``tests/integration/test_migrations.py``
    drift guard, ADR 0030).
    """

    __tablename__ = "projection_checkpoint"

    # Target name, e.g. "neo4j" — the projection target this watermark belongs to.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Highest statement.seq folded into this target. Initialises to 0 (no rows consumed yet).
    last_statement_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), nullable=False
    )
    # Highest decision.seq folded into this target. Initialises to 0.
    last_decision_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), nullable=False
    )
    # Last fold timestamp (server-assigned on INSERT; manually updated on subsequent folds).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LlmEgressRecord(Base):
    """Durable, append-only LLM-egress audit row (Gate F2 / ADR 0105).

    Makes the LLM-egress accountability record (ADR 0104's L1 stdlib-logging audit) durable
    and tamper-evident: an INSERT-only Postgres table sitting ALONGSIDE ``egress_log.emit()``
    (unchanged), written by the same single gateway choke point at the same two points
    (pre-call, post-call).

    Two row kinds per crossing (``phase``), correlated by a shared ``call_id``:

    * an **attempt** row (pre-call): ``content_fingerprint`` (a sha256 hex digest over the
      canonicalized outbound messages — NEVER the message content itself) and an optional,
      **caller-declared** ``entity_manifest`` (never content-derived, SF-2); token columns
      NULL.
    * a **completed** row (post-call): token usage from the response; ``content_fingerprint``
      / ``entity_manifest`` NULL.

    Append-only invariants (mirrors the ADR-0099 statement/decision spine idiom):
    * Only ``session.add`` INSERTs are ever issued for this table — no UPDATE, no DELETE,
      no ``session.delete``, ever (see :mod:`worldmonitor.llm.egress_audit`).
    * No column ever holds message content or the api key (ADR 0091 §3, extended);
      ``content_fingerprint`` is the durable, non-leaking stand-in.

    **No ``seq`` IDENTITY column** (SF-3): nothing consumes an ordering watermark over this
    table (no projector, no incremental exporter); ``created_at`` + ``call_id`` suffice for
    ordering display and pre/post correlation. Deliberately avoids ADR 0100's dialect-guarded
    ``before_insert`` SQLite fallback trap — do NOT register a ``before_insert`` listener for
    this model.

    This model and migration ``0011_llm_egress_audit`` MUST agree byte-for-byte
    (``tests/integration/test_migrations.py`` drift guard, ADR 0030).
    """

    __tablename__ = "llm_egress"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Shared uuid4 correlating the attempt + completed rows of ONE crossing.
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    # "attempt" (pre-call) | "completed" (post-call)
    phase: Mapped[str] = mapped_column(String(16), index=True)
    mode: Mapped[str] = mapped_column(String(32))
    confidentiality: Mapped[str] = mapped_column(String(64))
    target_host: Mapped[str] = mapped_column(String(255))
    data_left_perimeter: Mapped[bool] = mapped_column(Boolean)
    model: Mapped[str] = mapped_column(String(255))
    caller_tag: Mapped[str] = mapped_column(String(255))
    # Attempt-row columns — NULL on a completed row.
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)
    entity_manifest: Mapped[list[str] | None] = mapped_column(JSONB, default=None)
    # Completed-row columns — NULL on an attempt row.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    total_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# SQLite ``seq`` fallback (ADR 0100 D1) — test-dialect compatibility only.
# ---------------------------------------------------------------------------
# ``StatementRecord.seq`` / ``DecisionRecord.seq`` are server-generated via Postgres
# ``GENERATED ... AS IDENTITY`` — durable + monotonic, the projector's outbox watermark. Postgres
# omits the column from INSERT and the server fills it. SQLite (fast unit tests) does NOT honour
# ``Identity`` on a non-PK column, so an ORM insert would leave ``seq`` NULL and violate NOT NULL.
# This ``before_insert`` listener supplies a client-side monotonic fallback for SQLite ONLY; on
# Postgres it is a no-op (``seq`` stays unset → the server IDENTITY generates it), so the production
# monotonic/durability guarantee the projector relies on is untouched. It changes no DDL, so the
# migration and the drift guard are unaffected.
_sqlite_seq_counter = itertools.count(1)


def _assign_sqlite_seq(_mapper: Any, connection: Any, target: Any) -> None:
    if target.seq is None and connection.dialect.name == "sqlite":
        target.seq = next(_sqlite_seq_counter)


event.listen(StatementRecord, "before_insert", _assign_sqlite_seq)
event.listen(DecisionRecord, "before_insert", _assign_sqlite_seq)
