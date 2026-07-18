"""``_redact_queue``'s connector-scoped SQL pre-filter (rereview 2026-07-11 finding #12).

The pre-filter must be a strict SUPERSET of the parse-based match: rows enqueued by the erased
source's connector are still parse-checked (dataset scoping is decided by the FtM provenance
round-trip exactly as before), while rows from other connectors are skipped without an FtM parse.
The trust base is the single enqueue path (``runner/ingest.py::run_ingest``) stamping the row's
``connector_id`` column and the provenance ``source_id = "<connector_id>:<dataset>"`` from the
same connector run — the same structural scoping ``_redact_dead_letters`` already relies on via
the landing-URI prefix.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.erasure import _redact_queue
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

_RETRIEVED_AT = "2026-07-18T00:00:00Z"


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _sqlite_sessions() -> tuple[Any, sessionmaker[Session]]:
    engine = make_engine("sqlite:///:memory:")
    create_all(engine)
    return engine, session_factory(engine)


def _entity(member_id: str, *, source_id: str) -> FtmEntity:
    entity = make_entity(
        {"id": member_id, "schema": "Person", "properties": {"name": ["Redact Probe"]}}
    )
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{member_id}.json",
        ),
    )


def _queue_item(connector_id: str, raw_entity: dict[str, Any]) -> ErQueueItem:
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id=connector_id,
        entity_id=str(raw_entity.get("id")) or None,
        raw_entity=raw_entity,
        source_record=f"s3://landing/{uuid.uuid4()}.json",
        status="resolved",
    )


def test_redacts_only_the_matching_source_and_keeps_siblings() -> None:
    """Same connector / other dataset stays parse-scoped; other connectors stay untouched."""
    _engine, sessions = _sqlite_sessions()
    with sessions() as session:
        target = _queue_item("conna", _entity("m-1", source_id="conna:x").to_dict())
        sibling = _queue_item("conna", _entity("m-2", source_id="conna:y").to_dict())
        other = _queue_item("connb", _entity("m-3", source_id="connb:z").to_dict())
        session.add_all([target, sibling, other])
        session.commit()

        assert _redact_queue(session, "conna:x") == 1
        session.commit()

        rows = {r.id: r for r in session.execute(select(ErQueueItem)).scalars()}
        assert rows[target.id].raw_entity == {"erased": True, "source_id": "conna:x"}
        assert rows[sibling.id].raw_entity["schema"] == "Person"
        assert rows[other.id].raw_entity["schema"] == "Person"


def test_other_connectors_are_never_parsed(caplog: Any) -> None:
    """The SQL pre-filter skips foreign-connector rows BEFORE the FtM parse: an un-parseable
    foreign row must produce no 'un-parseable' warning (pre-fix it was parsed and warned)."""
    _engine, sessions = _sqlite_sessions()
    with sessions() as session:
        poisoned = _queue_item("connb", {"schema": "NoSuchSchema", "id": "m-bad"})
        session.add(poisoned)
        session.commit()

        with caplog.at_level(logging.WARNING, logger="worldmonitor.erasure"):
            assert _redact_queue(session, "conna:x") == 0
        assert "un-parseable" not in caplog.text
        session.commit()

        row = session.execute(select(ErQueueItem)).scalars().one()
        assert row.raw_entity == {"schema": "NoSuchSchema", "id": "m-bad"}
