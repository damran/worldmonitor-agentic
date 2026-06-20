"""Base connector for FtM-native bulk datasets.

When a source already publishes FollowTheMoney (OpenSanctions, ftm-exports, …),
mapping is near-identity: parse one FtM object per record, validate it against the
schema, and stamp provenance. Subclasses supply the manifest, the config schema,
and ``collect()`` (where/how to fetch the dataset).
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.plugins.base import Connector, RawRecord
from worldmonitor.provenance.model import Provenance, stamp


class FtmBulkConnector(Connector):
    """Connector base for sources whose records are already FtM entities."""

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Parse, validate, and provenance-stamp one FtM entity from ``record``."""
        payload = json.loads(record.data)
        entity = validate_or_raise(payload)
        return [stamp(entity, provenance)]
