"""SQLAlchemy 2.0 models — the relational side of the platform.

Phase 1 needs two tables: the tenant-scoped connector-instance registry that the
Integrations page reads/writes (config stored **encrypted**, see
:mod:`worldmonitor.db.crypto`), and the entity-resolution queue connectors push
mapped candidates onto. Every row carries ``tenant_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
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

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    connector_id: Mapped[str] = mapped_column(String(128))
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
