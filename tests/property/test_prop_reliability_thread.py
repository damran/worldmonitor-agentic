"""Property pin: driver reliability threading is an exact grade pass-through.

Gate S-4 slice 1 (reliability threading, ADR 0120 / GATE_S4_RANSOMWARE_LIVE_SPEC.md §5, §9).
The implementation landed in commit ``c51191f`` (RED tests: ``tests/unit/test_seed.py``,
``tests/unit/test_driver_reliability.py``, ``tests/integration/test_migrations.py``, AC1-AC3).
This file is a **pin**, not a RED gate test — it is GREEN on the current tree. Per CLAUDE.md's
"Property tests are part of the gate, not optional" build discipline: this gate touches the
provenance invariant (G1 — every enqueued entity carries a reliability grade), so it gets a
``@given`` metamorphic pin here, not just the example-based AC1-AC3 tests.

Metamorphic property: for ANY grade stored on ``ConnectorInstance.reliability`` (the Admiralty
``A``-``F`` letters, ``""``, arbitrary short unicode, or ``NULL``), the ``wm_prov_*`` reliability
the driver stamps on the entity it enqueues is an **exact pass-through** of that grade — except
``NULL``, which falls back to ``"B"`` (byte-identical to every pre-S4 connector,
``runner/driver.py``: ``reliability if reliability is not None else "B"``). In particular an
empty string must stay ``""`` — it must never be coerced to ``"B"`` by a stray falsy-check.

Drives the SAME real claim->ingest path as ``tests/unit/test_driver_reliability.py``
(``IngestDriver.run_due_ingests`` -> ``_ingest_instance`` -> ``run_ingest``), reproduced minimally
here as a small self-contained harness (neither ``tests/property`` nor ``tests/unit`` is a
package rooted under ``tests/`` — a cross-module import would be import-mode-fragile, so the
harness is inlined rather than shared) — same idiom, same fakes, same in-memory-SQLite-per-example
isolation as ``tests/property/test_prop_landing_gc_reference_safety.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
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

# Each example spins a fresh in-memory SQLite DB and runs one full driver ingest tick — heavier
# than a pure-function property, so max_examples is kept modest (in line with the repo's other
# DB-backed property tests, e.g. test_prop_landing_gc_reference_safety.py's P-ER-STATUS).
# deadline=None for the same reason (SQLite create_all + a full ingest tick can exceed the
# default 200ms deadline on a loaded runner).
_SETTINGS = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# SQLite JSONB shim (same idiom as tests/unit/test_seed.py / test_driver_reliability.py):
# Base.metadata spans JSONB-columned tables (ErQueueItem.raw_entity, TaskRun.stats, ...).
@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


class _StubConnector(Connector):
    """Emits exactly one Company entity — enough to inspect the stamped provenance."""

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


def _stamped_reliability_for(grade: str | None) -> str:
    """Drive one full claim->ingest tick for an instance carrying ``grade``; return the
    reliability the driver actually stamped on the entity it enqueued.

    A fresh in-memory SQLite engine per call gives full isolation across Hypothesis examples
    (including during shrinking); disposed in ``finally`` — a leaked engine on a
    failing/shrinking example exhausts connections and masks the real assertion (the repo's
    known heavy-``@given``-RED-test trap).
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    try:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        registry = Registry()
        registry.register(_StubConnector("stub-prop"))
        cipher = ConfigCipher(ConfigCipher.generate_key())
        driver = IngestDriver(
            sessions=sessions,
            landing=_FakeLanding(),  # type: ignore[arg-type]
            neo4j=MagicMock(),
            registry=registry,
            cipher=cipher,
            settings=Settings(_env_file=None, environment="test"),  # type: ignore[call-arg]
        )

        instance_id = str(uuid.uuid4())
        with sessions() as session:
            session.add(
                ConnectorInstance(
                    id=instance_id,
                    connector_id="stub-prop",
                    config_encrypted=cipher.encrypt(json.dumps({"dataset": "d"})),
                    status="enabled",
                    reliability=grade,
                )
            )
            session.commit()

        ran = driver.run_due_ingests(now=_NOW)
        assert instance_id in ran, "the freshly-seeded instance must have been due and run"

        with sessions() as session:
            item = session.execute(select(ErQueueItem)).scalar_one()
            entity = make_entity(item.raw_entity)
        provenance = get_provenance(entity)
        assert provenance is not None, "map() must stamp provenance on every enqueued entity (G1)"
        return provenance.reliability
    finally:
        engine.dispose()


# Admiralty grades explicitly weighted, plus "" and arbitrary short unicode (bounded to the
# column's declared width, String(16)), plus None (a NULL reliability column).
_ADMIRALTY_GRADE = st.sampled_from(["A", "B", "C", "D", "E", "F"])
_SHORT_UNICODE = st.text(min_size=0, max_size=16)
_GRADE = st.one_of(st.none(), _ADMIRALTY_GRADE, st.just(""), _SHORT_UNICODE)


@given(grade=_GRADE)
@_SETTINGS
def test_p_s4_reliability_thread_is_exact_pass_through(grade: str | None) -> None:
    """For any grade the driver reads off ConnectorInstance.reliability, the reliability
    stamped on the enqueued entity's provenance equals that grade exactly — except NULL, which
    maps to "B" and ONLY "B" (never any other substitution)."""
    stamped = _stamped_reliability_for(grade)
    expected = grade if grade is not None else "B"
    assert stamped == expected, (
        f"grade={grade!r} on ConnectorInstance.reliability must stamp reliability={expected!r} "
        f"on the enqueued entity's provenance — got {stamped!r}"
    )
