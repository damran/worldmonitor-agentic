"""Integration test: a connector-instance row round-trips with encrypted config.

Runs against an ephemeral Postgres (testcontainers).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ConnectorInstance

pytestmark = pytest.mark.integration


def test_connector_instance_roundtrip_encrypted(postgres_dsn: str) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)

    cipher = ConfigCipher(ConfigCipher.generate_key())
    config = {"dataset": "us_ofac_sdn", "api_key": "super-secret"}  # pragma: allowlist secret
    blob = cipher.encrypt(json.dumps(config))

    sessions = session_factory(engine)
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id="inst-1",
                connector_id="opensanctions",
                config_encrypted=blob,
                status="enabled",
            )
        )
        session.commit()

    with sessions() as session:
        row = session.execute(
            select(ConnectorInstance).where(ConnectorInstance.id == "inst-1")
        ).scalar_one()
        assert row.connector_id == "opensanctions"
        assert row.created_at is not None
        # Stored ciphertext must not leak the plaintext secret.
        assert "super-secret" not in row.config_encrypted  # pragma: allowlist secret
        assert json.loads(cipher.decrypt(row.config_encrypted)) == config

    engine.dispose()
