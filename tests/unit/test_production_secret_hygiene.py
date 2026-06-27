"""Gate M-2 — production secret hygiene (ADR 0061).

Two invariants, failing-test-first (RED now, GREEN after the builder):

1. ``Settings.validate_production_secrets()`` fails CLOSED in any non-``development``
   environment when a secret the app actually reads is still a recognizable
   placeholder/weak value (``config_encryption_key`` empty or ``change-me``; a
   ``change-me`` / known-guessable token in ``postgres_dsn`` / ``redis_url`` /
   service passwords). In ``development`` placeholders are allowed (never raises),
   and a non-dev deployment with real, strong secrets must pass.

2. ``deploy/compose.yaml`` publishes each backend store port
   (postgres 5432 / neo4j 7687 / minio 9000 / redis 6379) bound to the host
   loopback (``127.0.0.1:<port>:<port>``), not to all interfaces.

Settings is built with ``_env_file=None`` (mirroring ``tests/unit/test_settings.py``)
so these assertions are independent of a developer's local ``.env``.

Exact ``Settings`` field names this oracle pins (the builder must match):
``environment``, ``config_encryption_key``, ``postgres_dsn``, ``redis_url``,
``neo4j_password``, ``minio_secret_key``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "deploy" / "compose.yaml"

# A real, well-formed Fernet key — the validator must accept this in non-dev.
_STRONG_KEY = ConfigCipher.generate_key()
# A non-placeholder, non-guessable DSN/URL for the strong-secret path.
_STRONG_DSN = "postgresql://wm_app:Sapphire-Kestrel-92af-Quartz@db.internal:5432/worldmonitor"
_STRONG_REDIS = "redis://:Tundra-Marble-71cd-Lantern@cache.internal:6379/0"


def _settings(**overrides: object) -> Settings:
    """Build a Settings instance independent of any local .env (see test_settings.py)."""
    base: dict[str, object] = {
        "_env_file": None,
        "config_encryption_key": _STRONG_KEY,
        "postgres_dsn": _STRONG_DSN,
        "redis_url": _STRONG_REDIS,
        "neo4j_password": "Granite-Willow-33be",
        "minio_secret_key": "Cobalt-Heron-58da",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Settings.validate_production_secrets — fail closed in non-dev
# --------------------------------------------------------------------------- #


def test_production_placeholder_encryption_key_raises() -> None:
    """Non-dev + ``config_encryption_key="change-me"`` must fail closed."""
    settings = _settings(environment="production", config_encryption_key="change-me")
    with pytest.raises(ValueError):
        settings.validate_production_secrets()


def test_production_empty_encryption_key_raises() -> None:
    """Non-dev + empty ``config_encryption_key`` must fail closed."""
    settings = _settings(environment="production", config_encryption_key="")
    with pytest.raises(ValueError):
        settings.validate_production_secrets()


def test_production_placeholder_in_postgres_dsn_raises() -> None:
    """A ``change-me`` placeholder password baked into the Postgres DSN must fail closed."""
    settings = _settings(
        environment="production",
        postgres_dsn="postgresql://worldmonitor:change-me@postgres:5432/worldmonitor",
    )
    with pytest.raises(ValueError):
        settings.validate_production_secrets()


def test_production_placeholder_in_redis_url_raises() -> None:
    """A ``change-me`` placeholder password in the Redis URL must fail closed."""
    settings = _settings(
        environment="production",
        redis_url="redis://:change-me@redis:6379/0",
    )
    with pytest.raises(ValueError):
        settings.validate_production_secrets()


def test_production_guessable_token_in_dsn_raises() -> None:
    """A known-guessable token (e.g. ``worldmonitor123``) in a secret must fail closed."""
    settings = _settings(
        environment="production",
        postgres_dsn="postgresql://worldmonitor:worldmonitor123@postgres:5432/worldmonitor",
    )
    with pytest.raises(ValueError):
        settings.validate_production_secrets()


def test_staging_environment_also_fails_closed() -> None:
    """Any non-``development`` environment (not just ``production``) is guarded."""
    settings = _settings(environment="staging", config_encryption_key="change-me")
    with pytest.raises(ValueError):
        settings.validate_production_secrets()


def test_development_with_placeholders_does_not_raise() -> None:
    """In ``development`` placeholders are allowed: the validator returns None."""
    settings = _settings(
        environment="development",
        config_encryption_key="change-me",
        postgres_dsn="postgresql://worldmonitor:change-me@localhost:5432/worldmonitor",
        redis_url="redis://:change-me@localhost:6379/0",
    )
    assert settings.validate_production_secrets() is None


def test_test_environment_with_placeholders_does_not_raise() -> None:
    """``test`` is a LOCAL environment (the unit suite boots create_app with environment='test' and
    no .env) — placeholders are allowed, the validator returns None. Pins the CI-safety fix."""
    settings = _settings(
        environment="test",
        config_encryption_key="",
        postgres_dsn="postgresql://worldmonitor:change-me@localhost:5432/worldmonitor",
    )
    assert settings.validate_production_secrets() is None


def test_unknown_environment_fails_closed() -> None:
    """An unrecognized/typo'd environment (not development/test) enforces — fail CLOSED."""
    settings = _settings(environment="prod", config_encryption_key="change-me")
    with pytest.raises(ValueError):
        settings.validate_production_secrets()


def test_production_with_strong_secrets_does_not_raise() -> None:
    """Non-dev + real Fernet key + non-placeholder DSN/URL/passwords: no raise."""
    settings = _settings(environment="production")
    assert settings.validate_production_secrets() is None


# --------------------------------------------------------------------------- #
# deploy/compose.yaml — backend stores bound to host loopback only
# --------------------------------------------------------------------------- #

_LOOPBACK_GUARDED = {
    "postgres": "5432",
    "neo4j": "7687",
    "minio": "9000",
    "redis": "6379",
}


def _published_mappings(service: dict) -> list[str]:
    """Normalize a service's ``ports:`` entries to a list of 'host[:...]' strings."""
    mappings: list[str] = []
    for entry in service.get("ports", []):
        if isinstance(entry, str):
            mappings.append(entry)
        elif isinstance(entry, dict):
            # long-form: published/target (+ optional host_ip)
            host_ip = entry.get("host_ip", "")
            published = entry.get("published", "")
            mappings.append(f"{host_ip}:{published}" if host_ip else str(published))
    return mappings


@pytest.mark.parametrize(("service_name", "port"), sorted(_LOOPBACK_GUARDED.items()))
def test_backend_store_port_is_loopback_bound(service_name: str, port: str) -> None:
    """Each backend store publishes its port bound to 127.0.0.1, not all interfaces."""
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = compose["services"][service_name]
    mappings = _published_mappings(service)

    # Find the mapping that publishes the guarded port and assert it is loopback-bound.
    matching = [m for m in mappings if f":{port}" in m or m.endswith(port)]
    assert matching, f"{service_name} does not publish port {port}: {mappings!r}"
    for mapping in matching:
        if port in mapping:
            assert mapping.startswith("127.0.0.1:"), (
                f"{service_name} publishes {mapping!r} on all interfaces; "
                f"expected loopback bind '127.0.0.1:{port}:{port}'"
            )
