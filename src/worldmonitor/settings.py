"""Application settings — 12-factor: every value comes from the environment.

Field names map case-insensitively to env vars (e.g. ``zitadel_domain`` <-
``ZITADEL_DOMAIN``). Nothing here carries a real secret; defaults are safe
placeholders so the app can boot and serve ``/health`` before the stack is
fully provisioned. See ``.env.example``.
"""

from functools import lru_cache

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
