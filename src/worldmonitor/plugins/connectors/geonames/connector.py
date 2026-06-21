"""GeoNames connector — reference gazetteer anchored on geonames_id.

GeoNames publishes per-country tab-separated dumps at ``download.geonames.org``.
Each feature becomes an FtM ``Address`` (the closest FtM-native geo schema; a
``wm:Place`` extension is a noted future refinement) carrying the ``geonames_id``
canonical anchor. A passive ``EXTERNAL_IMPORT`` connector — it never writes to the
graph. Supports a local ``path`` override for environments where the live source
is unreachable.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.plugins.base import (
    Capability,
    Connector,
    Kind,
    Manifest,
    Mode,
    RawRecord,
    Status,
)
from worldmonitor.provenance.model import Provenance, stamp

_BASE_URL = "https://download.geonames.org/export/dump"
_HTTP_TIMEOUT = 120.0

# Column indices in the GeoNames dump (tab-separated, see its readme.txt).
_COL_GEONAMEID = 0
_COL_NAME = 1
_COL_LAT = 4
_COL_LON = 5
_COL_COUNTRY = 8


class GeoNamesConnector(Connector):
    """Loads GeoNames places (FtM Address) with the ``geonames_id`` anchor."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="geonames",
            name="GeoNames",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description="Reference gazetteer; anchors places to GeoNames IDs.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Yield one raw record per GeoNames dump line for the configured country."""
        self.validate_config(config)
        retrieved_at = datetime.now(UTC).isoformat()
        local_path = config.get("path")
        if local_path:
            text = Path(str(local_path)).read_text("utf-8")
        else:
            text = self._download(str(config["country"]).upper())
        for line in text.splitlines():
            if not line.strip():
                continue
            geoname_id = line.split("\t", 1)[0]
            yield RawRecord(
                key=geoname_id,
                data=line.encode("utf-8"),
                retrieved_at=retrieved_at,
                content_type="text/tab-separated-values",
            )

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Parse one GeoNames dump line into an FtM Address with its geonames_id anchor."""
        columns = record.data.decode("utf-8").split("\t")
        if len(columns) <= _COL_COUNTRY:
            return []
        geoname_id = columns[_COL_GEONAMEID].strip()
        name = columns[_COL_NAME].strip()
        if not geoname_id or not name:
            return []
        properties: dict[str, list[str]] = {"name": [name]}
        if columns[_COL_COUNTRY].strip():
            properties["country"] = [columns[_COL_COUNTRY].strip().lower()]
        if columns[_COL_LAT].strip():
            properties["latitude"] = [columns[_COL_LAT].strip()]
        if columns[_COL_LON].strip():
            properties["longitude"] = [columns[_COL_LON].strip()]
        entity = validate_or_raise(
            {
                "id": f"geonames-{geoname_id}",
                "schema": "Address",
                "properties": properties,
                "datasets": ["geonames"],
            }
        )
        set_anchor(entity, "geonames_id", geoname_id)
        return [stamp(entity, provenance)]

    @staticmethod
    def _download(country: str) -> str:
        response = httpx.get(
            f"{_BASE_URL}/{country}.zip", timeout=_HTTP_TIMEOUT, follow_redirects=True
        )
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            return archive.read(f"{country}.txt").decode("utf-8")
