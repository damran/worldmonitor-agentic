"""Application settings — 12-factor: every value comes from the environment.

Field names map case-insensitively to env vars (e.g. ``zitadel_domain`` <-
``ZITADEL_DOMAIN``). Nothing here carries a real secret; defaults are safe
placeholders so the app can boot and serve ``/health`` before the stack is
fully provisioned. See ``.env.example``.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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

    # --- Driver supervision & containerization (Gate B-4c / ADR 0051) ---
    # The driver writes a last-tick heartbeat FILE (per-container; not a table) once per loop
    # iteration; `python -m worldmonitor.runner.driver --healthcheck` reads ONLY this file and
    # exits 0 (alive) / 1 (missing-or-stale) — the container HEALTHCHECK. A stale heartbeat makes
    # a stalled pipeline detectable even while /health still echoes ok (the audit's false-confidence
    # signal). ``driver_heartbeat_stale_seconds`` is a safe multiple of ``driver_tick_seconds``
    # (default 90s ≈ 3 missed 30s ticks) so a single slow tick does not flap the healthcheck.
    driver_heartbeat_path: str = "/var/run/worldmonitor/driver.heartbeat"
    driver_heartbeat_stale_seconds: float = Field(default=90.0, gt=0)
    # Per-store bound on each /ready probe (Postgres/Neo4j/MinIO) so a hung store cannot hang
    # the readiness endpoint forever (spec §3.4).
    readiness_probe_timeout_seconds: float = Field(default=5.0, gt=0)

    # --- Fail-closed sensitivity guard (Gate E / ADR 0047) ---
    # The guard's sensitive-topic SET is loaded programmatically from FtM's own
    # ``registry.topic.RISKS`` (never configured — deny-by-default cannot be opened);
    # these knobs tune only the Stage-2 k-hop traversal and the Stage-3 Chow abstain band.
    #   sensitivity_khop_depth   — Stage-2 graph traversal depth, f-string-inlined into the
    #                              ``[*1..k]`` variable-length bound (int-validated, never a
    #                              $param). ``0`` disables Stage 2 (the kill-switch).
    #   sensitivity_abstain_low  — Stage-3 Chow band lower bound (INCLUSIVE).
    #   sensitivity_abstain_high — Stage-3 Chow band upper bound (EXCLUSIVE). ``low == high``
    #                              ⇒ an empty half-open interval ⇒ the band is OFF (the default).
    # The band defaults to ``[0.92, 0.92)`` (empty) so slice-2 over-parks NOTHING until a human
    # tunes it; ``low > high`` is rejected by the validator below (an inverted, nonsensical band).
    # le=4 caps the f-string-inlined ``[*1..k]`` traversal (exponential in k) so a misconfiguration
    # cannot stall the resolve hot path on a dense graph (spec §15 / ADR 0047 §6); 0 = kill-switch.
    sensitivity_khop_depth: int = Field(default=1, ge=0, le=4)
    sensitivity_abstain_low: float = Field(default=0.92, ge=0.0, le=1.0)
    sensitivity_abstain_high: float = Field(default=0.92, ge=0.0, le=1.0)

    # --- Secrets ---
    # Fernet key for encrypting connector-instance config at rest. Required in
    # any environment that persists connector configs; empty default fails fast.
    config_encryption_key: str = ""

    @model_validator(mode="after")
    def _validate_abstain_band(self) -> "Settings":
        """Reject an inverted Chow abstain band (``low > high``) — spec §6 / ADR 0047 Decision 6.

        ``low == high`` is allowed (the OFF / empty-band configuration); only a strictly inverted
        band is nonsensical. The band never touches ``DEFAULT_MERGE_THRESHOLD`` — it is a distinct
        park-vs-promote axis on an already-formed cluster (spec §3.4).
        """
        if self.sensitivity_abstain_low > self.sensitivity_abstain_high:
            raise ValueError(
                "sensitivity_abstain_low must be <= sensitivity_abstain_high "
                f"(got low={self.sensitivity_abstain_low}, high={self.sensitivity_abstain_high})"
            )
        return self

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
