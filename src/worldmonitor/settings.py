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

# Placeholder/weak markers a non-development boot refuses (ADR 0061). A secret the app
# reads is rejected if it is empty or contains one of these — the realistic "forgot to
# replace change-me" footgun. NOT an entropy/strength check (a strong-looking but weak
# password passes anyway); a real Fernet key / any non-placeholder value is accepted.
_PLACEHOLDER_SECRET_MARKERS = ("change-me", "worldmonitor123")

# Local/CI environments where placeholder secrets are allowed (the unit suite boots create_app with
# environment="test"). Anything NOT in this set — production, staging, or an unknown/typo'd value —
# enforces the placeholder check (fail CLOSED on unknown). ADR 0061.
_LOCAL_ENVIRONMENTS = ("development", "test")


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
    # Confidential-client secret for the browser OIDC authorization-code flow (ADR 0068). Empty
    # default; fail-closed in prod when auth is configured (validate_production_secrets).
    zitadel_client_secret: str = ""
    # Signs the Starlette SessionMiddleware cookie that carries the browser session principal
    # (ADR 0068). An empty/guessable key would let anyone forge a session — fail-closed in prod.
    session_secret_key: str = ""
    # Absolute public base URL (e.g. ``https://wm.example.com``) used to build the absolute OIDC
    # ``redirect_uri`` (``app_base_url`` + ``/auth/callback``) — NOT a Host-derived URL (ADR 0068).
    app_base_url: str = ""

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
    # On ingest FAILURE the instance stays retryable (status="enabled") and is rescheduled with an
    # exponential backoff (ADR 0054): backoff = min(base * 2**(consecutive_failures-1), max). A
    # success resets the streak (back to ``ingest_cadence_seconds``). Connector SCHEDULING only.
    ingest_retry_base_seconds: int = Field(default=60, gt=0)
    ingest_retry_max_seconds: int = Field(default=3600, gt=0)
    # Finished (ok/error) task_run rows older than this are pruned on driver startup
    # so the history table does not grow without bound (ADR 0029 follow-up). 0 disables.
    task_run_retention_days: int = Field(default=30, ge=0)
    # Dead-letter (``ingest_dead_letter``) rows older than this are pruned on driver startup so the
    # replayable error-audit table does not grow without bound (Gate B-4d / ADR 0053). 0 disables.
    dead_letter_retention_days: int = Field(default=30, ge=0)

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

    # --- GeoNames connector: local-path confinement (Gate H-6/H-7 / ADR 0052) ---
    # The GeoNames connector accepts an optional local ``path`` override (load-bearing for the
    # VA.txt fixture + offline dev). Left unconstrained it is an arbitrary file read (LFI), so the
    # runtime confines it to an allowlist DIRECTORY — the security boundary is THIS check, not the
    # JSON schema (which doubles as the self-service UI-form driver and is bypassable by a direct
    # caller).
    #   geonames_allowed_path_dir — allowlist base dir for the local ``path`` override. DEFAULT
    #     EMPTY ⇒ the ``path`` override is rejected entirely (DEFAULT-DENY); production that never
    #     sets it cannot be tricked into a local read. Dev/test points it at the fixtures dir. The
    #     runtime resolves both the allowlist and the candidate via realpath (defeating ``..`` AND
    #     symlinks) and requires the candidate to be inside the allowlist.
    #   geonames_max_path_bytes — defense-in-depth size cap (256 MiB) on a confined local read;
    #     an over-cap file is rejected before it is read.
    geonames_allowed_path_dir: str = ""
    geonames_max_path_bytes: int = Field(default=268_435_456, gt=0)

    # --- ACTIVE heavy-tool sandbox gate (ADR 0072 §1) ---
    # A CliTool connector declares a ``sandbox`` level ("subprocess" | "container"). A
    # ``sandbox=="container"`` connector (e.g. nmap — an un-sandboxed network scanner from the host
    # violates "heavy CLI tools in containers") is REFUSED by the operator-run path until a real
    # container/egress sandbox exists. This flag is that gate; DEFAULT ``False`` (no container
    # runtime in v1) means every container-level tool's EXECUTION is refused
    # (SandboxUnavailableError -> REST 409) BEFORE any subprocess/landing. Subprocess-level tools
    # (whois/dig) are unaffected. When the Stage-4 container sandbox lands, flipping this True lets
    # those tools run — no connector change. Not person-affecting; single-tenant.
    container_sandbox_enabled: bool = False

    # --- Secrets ---
    # Fernet key for encrypting connector-instance config at rest. Required in
    # any environment that persists connector configs; empty default fails fast.
    config_encryption_key: str = ""
    # Comma/whitespace-separated decryption-only OLD keys (ADR 0058). During a key
    # rotation the previous CONFIG_ENCRYPTION_KEY moves here so tokens written under it
    # still decrypt; empty default => one-key cipher == today's behaviour.
    config_encryption_key_fallbacks: str = ""

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

    def validate_production_secrets(self) -> None:
        """Fail closed outside the local/test environments on a placeholder secret (ADR 0061).

        LOCAL environments ``development``/``test`` allow placeholders (the unit suite boots
        ``create_app`` with ``environment="test"`` and no ``.env``) and return immediately. EVERY
        other value — ``production``, ``staging``, or an unrecognized/typo'd string — enforces (fail
        CLOSED on unknown): raise ``ValueError`` naming the field if a secret the app actually reads
        is a placeholder/weak value: ``config_encryption_key`` empty or ``change-me``; or
        ``change-me``/``worldmonitor123`` inside ``postgres_dsn``/``redis_url``/``neo4j_password``/
        ``minio_secret_key``. A plain marker check — NO entropy scoring; a real Fernet key passes.

        ADR 0068: ``session_secret_key`` is required UNCONDITIONALLY (the session cookie is signed
        on every boot, auth wired or not), while ``zitadel_client_secret`` is required only when
        auth is configured (``auth_configured`` — a confidential client cannot ship without it).
        """
        if self.environment in _LOCAL_ENVIRONMENTS:
            return

        if not self.config_encryption_key or self.config_encryption_key == "change-me":
            raise ValueError(
                "config_encryption_key is a placeholder/empty value; set a real secret "
                f"(non-development environment={self.environment!r} refuses placeholders, ADR 0061)"
            )

        guarded_fields = ("postgres_dsn", "redis_url", "neo4j_password", "minio_secret_key")
        for field in guarded_fields:
            value: str = getattr(self, field)
            for marker in _PLACEHOLDER_SECRET_MARKERS:
                if marker in value:
                    raise ValueError(
                        f"{field} contains placeholder/guessable marker {marker!r}; set a real "
                        f"secret (non-development environment={self.environment!r} refuses "
                        "placeholders, ADR 0061)"
                    )

        # UNCONDITIONAL (ADR 0068 security fix): the SessionMiddleware cookie is signed on EVERY
        # boot (with ``session_secret_key`` or, in dev/test only, a per-process random fallback),
        # whether or not OIDC is wired. An empty/guessable ``session_secret_key`` therefore lets
        # anyone forge a browser session REGARDLESS of ``auth_configured`` — so it is required
        # outside the local/test environments unconditionally (NOT gated behind auth_configured).
        if not self.session_secret_key or any(
            marker in self.session_secret_key for marker in _PLACEHOLDER_SECRET_MARKERS
        ):
            raise ValueError(
                "session_secret_key is a placeholder/empty value; set a real secret "
                f"(non-development environment={self.environment!r} refuses placeholders, ADR 0068)"
            )

        # AUTH-GATED (ADR 0068): the confidential-client secret only becomes load-bearing once the
        # browser OIDC flow is actually wired up (``zitadel_domain`` + ``client_id``) — a
        # confidential client cannot ship without its secret. With auth unconfigured no OIDC client
        # is built, so it is never read.
        if self.auth_configured and (
            not self.zitadel_client_secret
            or any(marker in self.zitadel_client_secret for marker in _PLACEHOLDER_SECRET_MARKERS)
        ):
            raise ValueError(
                "zitadel_client_secret is a placeholder/empty value but auth is configured; set a "
                f"real secret (non-development environment={self.environment!r} refuses "
                "placeholders, ADR 0068)"
            )

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
    def oidc_discovery_url(self) -> str:
        """Zitadel's OIDC discovery document (Authlib registers the client from it, ADR 0068)."""
        return (
            f"https://{self.zitadel_domain}/.well-known/openid-configuration"
            if self.zitadel_domain
            else ""
        )

    @property
    def auth_configured(self) -> bool:
        """True once Zitadel domain + client id are set (auth can be enforced)."""
        return bool(self.zitadel_domain and self.zitadel_client_id)


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
