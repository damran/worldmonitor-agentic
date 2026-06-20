"""OpenSanctions connector.

OpenSanctions publishes per-dataset ``entities.ftm.json`` (one FtM entity per
line) for free at ``data.opensanctions.org`` — FtM-native and zero-risk, the
ideal source to prove the Phase-1 spine. ``collect()`` streams the dataset to the
landing zone; ``map()`` (inherited from :class:`FtmBulkConnector`) is near-identity:
validate against the schema and stamp provenance. This is a passive
``EXTERNAL_IMPORT`` connector — it never writes to the graph.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import httpx

from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord, Status
from worldmonitor.plugins.ftm_bulk import FtmBulkConnector

_BASE_URL = "https://data.opensanctions.org/datasets/latest"
_HTTP_TIMEOUT = 120.0


class OpenSanctionsConnector(FtmBulkConnector):
    """Streams a FollowTheMoney dataset from OpenSanctions into the pipeline."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="opensanctions",
            name="OpenSanctions",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description="FtM-native sanctions / PEP / crime datasets from OpenSanctions.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Stream ``entities.ftm.json`` for the configured dataset, one record per line."""
        self.validate_config(config)
        dataset = str(config["dataset"])
        limit = config.get("limit")
        url = f"{_BASE_URL}/{dataset}/entities.ftm.json"
        retrieved_at = datetime.now(UTC).isoformat()

        count = 0
        with httpx.stream("GET", url, timeout=_HTTP_TIMEOUT, follow_redirects=True) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.strip():
                    continue
                key = self._record_key(line, fallback=count)
                yield RawRecord(key=key, data=line.encode("utf-8"), retrieved_at=retrieved_at)
                count += 1
                if limit is not None and count >= int(limit):
                    break

    @staticmethod
    def _record_key(line: str, *, fallback: int) -> str:
        """Derive a stable landing key from the entity id (data treated as untrusted)."""
        try:
            entity_id = json.loads(line).get("id")
        except (ValueError, AttributeError):
            entity_id = None
        return str(entity_id) if entity_id else f"record-{fallback}"
