"""Gate S-4 slice 1 (AC2) — the driver threads ConnectorInstance.reliability into run_ingest.

GATE_S4_RANSOMWARE_LIVE_SPEC.md §5 / §9: the live driver claim->ingest path
(``runner/driver.py:_ingest_instance``) reads the claimed instance's fields but, on the current
tree, never reads ``reliability`` and never passes it to ``run_ingest`` — so every source stamps
the hardcoded ``"B"`` default regardless of what the instance carries. This pins the fix: the
driver must read ``instance.reliability`` in the claim transaction and pass it through, with
``None`` on the instance falling back to ``"B"`` (byte-identical to every pre-S4 connector).

Drives the REAL claim->ingest path (``IngestDriver.run_due_ingests`` -> ``_ingest_instance`` ->
``run_ingest``) through a stubbed connector, against an in-memory SQLite database (no Docker
needed — the ingest pass never touches Neo4j, so it is mocked). Reads the stamped provenance back
off the entity the driver actually enqueued (``ErQueueItem.raw_entity``), not a mocked call arg —
so the assertion pins the real end-to-end behaviour, not just that *some* kwarg was passed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.models import Base, ConnectorInstance, ErQueueItem
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.plugins.base import Capability, Connector, Kind, Manifest, Mode, RawRecord
from worldmonitor.plugins.registry import Registry
from worldmonitor.provenance.model import Provenance, get_provenance, stamp
from worldmonitor.runner.driver import IngestDriver
from worldmonitor.settings import Settings

_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


# SQLite JSONB shim (same idiom as tests/unit/test_statements.py / test_seed.py): Base.metadata
# spans JSONB-columned tables (ErQueueItem.raw_entity, TaskRun.stats, ...).
@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


class _StubConnector(Connector):
    """Emits exactly one Company entity — just enough to inspect the stamped provenance."""

    def __init__(self, connector_id: str) -> None:
        self._id = connector_id

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id=self._id,
            name=self._id,
            version="0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        yield RawRecord(key="r0", data=b'{"n":1}', retrieved_at="2026-07-23T00:00:00Z")

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        entity = make_entity(
            {"id": record.key, "schema": "Company", "properties": {"name": ["Stub Co"]}}
        )
        return [stamp(entity, provenance)]


class _FakeLanding:
    """In-memory stand-in for LandingStore (duck-typed: ensure_bucket + put)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        self.objects[key] = data
        return f"s3://landing/{key}"


def _harness(connector: Connector) -> tuple[Any, sessionmaker[Session], IngestDriver, ConfigCipher]:
    """An IngestDriver backed by an in-memory SQLite DB. Neo4j is mocked — the ingest pass
    (``run_due_ingests`` -> ``_ingest_instance``) never touches it."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    registry = Registry()
    registry.register(connector)
    cipher = ConfigCipher(ConfigCipher.generate_key())
    driver = IngestDriver(
        sessions=sessions,
        landing=_FakeLanding(),  # type: ignore[arg-type]
        neo4j=MagicMock(),
        registry=registry,
        cipher=cipher,
        settings=Settings(_env_file=None, environment="test"),  # type: ignore[call-arg]
    )
    return engine, sessions, driver, cipher


def _add_instance(
    sessions: sessionmaker[Session],
    *,
    connector_id: str,
    cipher: ConfigCipher,
    reliability: str | None,
) -> str:
    instance_id = str(uuid.uuid4())
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id=instance_id,
                connector_id=connector_id,
                config_encrypted=cipher.encrypt(json.dumps({"dataset": "d"})),
                status="enabled",
                reliability=reliability,
            )
        )
        session.commit()
    return instance_id


def _enqueued_provenance(sessions: sessionmaker[Session]) -> Provenance:
    with sessions() as session:
        item = session.execute(select(ErQueueItem)).scalar_one()
        entity = make_entity(item.raw_entity)
    provenance = get_provenance(entity)
    assert provenance is not None, "map() must stamp provenance on every enqueued entity (G1)"
    return provenance


def test_driver_threads_instance_reliability_into_enqueued_provenance() -> None:
    """AC2: an instance with reliability='E' stamps 'E' on the entity the driver enqueues (not
    the hardcoded 'B' default)."""
    engine, sessions, driver, cipher = _harness(_StubConnector("stub-e"))
    try:
        _add_instance(sessions, connector_id="stub-e", cipher=cipher, reliability="E")

        ran = driver.run_due_ingests(now=_NOW)
        assert ran, "the freshly-seeded instance must have been due and run"

        provenance = _enqueued_provenance(sessions)
        assert provenance.reliability == "E", (
            "the driver must pass ConnectorInstance.reliability into run_ingest, not the "
            f"hardcoded 'B' default — got {provenance.reliability!r}"
        )
    finally:
        engine.dispose()


def test_driver_defaults_to_b_when_instance_reliability_is_unset() -> None:
    """AC2 (byte-identical default): reliability=None on the instance still stamps 'B' — every
    existing pre-S4 connector's behaviour is unchanged."""
    engine, sessions, driver, cipher = _harness(_StubConnector("stub-null"))
    try:
        _add_instance(sessions, connector_id="stub-null", cipher=cipher, reliability=None)

        ran = driver.run_due_ingests(now=_NOW)
        assert ran, "the freshly-seeded instance must have been due and run"

        provenance = _enqueued_provenance(sessions)
        assert provenance.reliability == "B", (
            f"NULL instance reliability must default to 'B' (byte-identical to pre-S4) — got "
            f"{provenance.reliability!r}"
        )
    finally:
        engine.dispose()
