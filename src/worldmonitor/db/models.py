"""SQLAlchemy 2.0 models — the relational side of the platform.

Phase 1 needs two tables: the tenant-scoped connector-instance registry that the
Integrations page reads/writes (config stored **encrypted**, see
:mod:`worldmonitor.db.crypto`), and the entity-resolution queue connectors push
mapped candidates onto. Every row carries ``tenant_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class ConnectorInstance(Base):
    """A configured, tenant-scoped instance of a connector plugin."""

    __tablename__ = "connector_instance"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    connector_id: Mapped[str] = mapped_column(String(128), index=True)
    # Fernet-encrypted JSON config blob — never stored in plaintext.
    config_encrypted: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String(32), default="disabled")
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    next_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ErQueueItem(Base):
    """A mapped FtM candidate awaiting entity resolution (L3 owns canonicalization)."""

    __tablename__ = "er_queue_item"
    # Idempotent enqueue (ADR 0029 / A6): the same landing record + FtM entity id
    # enqueues at most once per tenant, so a re-ingest after a crash/restart does
    # not double-enqueue. ``entity_id`` is NULL for an id-less entity; Postgres
    # treats NULLs as distinct, so those rare rows are not deduped.
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_record", "entity_id", name="uq_er_queue_dedup"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
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
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    canonical_id: Mapped[str] = mapped_column(String(255), index=True)
    source_ids: Mapped[list[str]] = mapped_column(JSONB)
    score: Mapped[float]
    decision: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestDeadLetter(Base):
    """A record that could not be landed or mapped during ingest (the dead-letter trail).

    ``run_ingest`` never lets one bad record abort the whole run (audit gap G8): a
    failure is recorded here and ingest continues. ``stage`` is ``"land"`` (the raw
    bytes could not be written to the landing zone — ``source_record`` is null) or
    ``"map"`` (the raw landed but mapping to FtM raised — ``source_record`` points at
    the landed bytes, replayable). ``error`` is a bounded exception summary. Every
    row carries ``tenant_id`` (the GDPR/audit invariant holds for failures too).
    """

    __tablename__ = "ingest_dead_letter"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
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
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
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
    (single-node assumption; the deferred lease replaces it under HA). Every row is
    tenant-scoped.
    """

    __tablename__ = "task_run"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    connector_instance_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    error: Mapped[str] = mapped_column(String, default="")


class ResolverJudgement(Base):
    """A DURABLE, tenant-scoped resolver judgement that survives the per-batch resolver.

    The per-batch resolver is ephemeral (ADR 0028, the G4 fix), so a judgement made
    in one batch evaporates. A human sign-off (ADR 0031) must persist: a REJECT writes
    a ``"negative"`` judgement so the records never re-merge; an APPROVE writes a
    ``"positive"`` one so they always do. EVERY batch's fresh resolver loads this
    tenant's judgements before clustering and they take precedence over Splink — so a
    reviewed cluster never re-parks. Tenant-scoped (``tenant_id``): one tenant's
    judgement can never bind another's resolution (the G4 invariant the global
    nomenklatura ledger violated). ``(tenant_id, left_id, right_id)`` is unique with
    ``left_id <= right_id`` (the pair is stored canonically ordered).
    """

    __tablename__ = "resolver_judgement"
    __table_args__ = (
        UniqueConstraint("tenant_id", "left_id", "right_id", name="uq_resolver_judgement_pair"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
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
    identity (a string in v0; Zitadel-backed in Phase 2). Tenant-scoped.
    """

    __tablename__ = "sign_off"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    canonical_id: Mapped[str] = mapped_column(String(255), index=True)
    source_ids: Mapped[list[str]] = mapped_column(JSONB)
    decision: Mapped[str] = mapped_column(String(16), index=True)  # "approved" | "rejected"
    approver: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
