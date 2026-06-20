"""SQLAlchemy 2.0 models — the relational side of the platform.

Phase 1 needs one table: the tenant-scoped connector-instance registry that the
Integrations page reads/writes. Config is stored **encrypted** (see
:mod:`worldmonitor.db.crypto`). Every row carries ``tenant_id``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
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
