"""Application settings — 12-factor: every value comes from the environment.

Field names map case-insensitively to env vars (e.g. ``zitadel_domain`` <-
``ZITADEL_DOMAIN``). Nothing here carries a real secret; defaults are safe
placeholders so the app can boot and serve ``/health`` before the stack is
fully provisioned. See ``.env.example``.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration, loaded from the environment (and ``.env`` in dev)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "development"

    # --- Zitadel / OIDC (auth-gated from day one) ---
    zitadel_domain: str = ""
    zitadel_client_id: str = ""

    # --- Backing services (URLs only; clients land in later phases) ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    postgres_dsn: str = "postgresql://worldmonitor:worldmonitor@localhost:5432/worldmonitor"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "worldmonitor"
    minio_secret_key: str = ""
    minio_secure: bool = False
    landing_bucket: str = "landing"

    # --- Entity resolution: catastrophic-merge guard (ADR 0024 → ADR 0031) ---
    # "block" (DEFAULT — production posture) parks flagged (oversized / PEP /
    # sanctioned) clusters in pending_review and never writes them; a human reviews
    # them via the sign-off mechanism (ADR 0031). "alert" was the TEMPORARY
    # build-phase mode that wrote flagged merges anyway with a durable merge_alerts
    # trail. The guard EVALUATION (resolution/review.py) is identical in both modes —
    # only the ACTION on a flagged cluster differs. Returned to "block" with human
    # sign-off, fulfilling the ADR 0024 obligation.
    merge_guard_mode: Literal["alert", "block"] = "block"

    # Max ER-queue candidates resolved per batch (ADR 0026). resolve_pending drains
    # the pending queue in windows of this size, committing per batch, so memory and
    # per-pass cost are bounded. Dedup is WITHIN a batch; cross-batch / incremental
    # dedup against already-resolved entities is deferred to the ER-streaming gate.
    resolve_batch_size: int = Field(default=1000, gt=0)

    # --- Ingest bounds + windowing (ADR 0027) ---
    # run_ingest drains a connector's collect() with these bounds so a stream that
    # never returns can't hang the run, and a long import commits its progress.
    #   ingest_commit_every   — land/map/enqueue this many records, then commit.
    #   ingest_timeout_seconds — wall-clock deadline on a single run; <= 0 disables.
    #   ingest_max_records     — optional hard cap on records pulled; None = no cap
    #                            (windowed commits already bound the blast radius).
    ingest_commit_every: int = Field(default=1000, gt=0)
    ingest_timeout_seconds: float = Field(default=1800.0, ge=0)
    ingest_max_records: int | None = Field(default=None, gt=0)

    # --- Ingest driver cadence (ADR 0029) ---
    # The long-running driver runs each enabled connector instance every
    # ``ingest_cadence_seconds`` and resolves the queue every
    # ``resolve_cadence_seconds`` (an INDEPENDENT cadence, not fired per ingest);
    # it wakes every ``driver_tick_seconds`` to check what is due. Single global
    # cadence for now (a per-connector cadence column is deferred).
    ingest_cadence_seconds: int = Field(default=3600, gt=0)
    resolve_cadence_seconds: int = Field(default=300, gt=0)
    driver_tick_seconds: float = Field(default=30.0, gt=0)
    # Finished (ok/error) task_run rows older than this are pruned on driver startup
    # so the history table does not grow without bound (ADR 0029 follow-up). 0 disables.
    task_run_retention_days: int = Field(default=30, ge=0)

    # --- Secrets ---
    # Fernet key for encrypting connector-instance config at rest. Required in
    # any environment that persists connector configs; empty default fails fast.
    config_encryption_key: str = ""

    @property
    def sqlalchemy_dsn(self) -> str:
        """The Postgres DSN with the psycopg (v3) driver SQLAlchemy expects."""
        if self.postgres_dsn.startswith("postgresql://"):
            return self.postgres_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
        return self.postgres_dsn

    @property
    def oidc_issuer(self) -> str:
        """The OIDC issuer URL Zitadel signs tokens with."""
        return f"https://{self.zitadel_domain}" if self.zitadel_domain else ""

    @property
    def oidc_jwks_uri(self) -> str:
        """Where to fetch Zitadel's signing keys."""
        return f"https://{self.zitadel_domain}/oauth/v2/keys" if self.zitadel_domain else ""

    @property
    def auth_configured(self) -> bool:
        """True once Zitadel domain + client id are set (auth can be enforced)."""
        return bool(self.zitadel_domain and self.zitadel_client_id)


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
