"""Ingest orchestration: collect → landing zone → map → ER queue.

The connector contract stays clean (collect raw, map to FtM-with-provenance);
this function wires it to storage and the database. Every raw record lands in
object storage first, so the provenance pointer is real before any entity is
enqueued. Connectors never touch the graph — candidates go to the ER queue and
L3 (resolution) owns canonicalization.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem
from worldmonitor.plugins.base import Connector
from worldmonitor.provenance.model import Provenance
from worldmonitor.storage.landing import LandingStore


@dataclass(frozen=True, slots=True)
class IngestStats:
    """Counts from one ingest run."""

    collected: int
    landed: int
    queued: int


def run_ingest(
    connector: Connector,
    config: Mapping[str, Any],
    *,
    tenant_id: str,
    landing: LandingStore,
    session: Session,
    reliability: str = "B",
) -> IngestStats:
    """Run ``connector`` for ``tenant_id``: land raw records and enqueue candidates."""
    connector_id = connector.manifest.connector_id
    dataset = str(config.get("dataset", ""))
    source_id = f"{connector_id}:{dataset}".rstrip(":")

    landing.ensure_bucket()
    collected = landed = queued = 0

    for record in connector.collect(config):
        collected += 1
        key = "/".join(filter(None, [tenant_id, connector_id, dataset, f"{record.key}.json"]))
        uri = landing.put(key, record.data, content_type=record.content_type)
        landed += 1

        provenance = Provenance(
            source_id=source_id,
            retrieved_at=record.retrieved_at,
            reliability=reliability,
            source_record=uri,
        )
        for entity in connector.map(record, provenance=provenance):
            session.add(
                ErQueueItem(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    raw_entity=entity.to_dict(),
                    source_record=uri,
                    status="pending",
                )
            )
            queued += 1

    session.commit()
    return IngestStats(collected=collected, landed=landed, queued=queued)
