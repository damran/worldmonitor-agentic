"""DigConnector — the second ACTIVE CLI-tool connector (read-only DNS lookup, ADR 0072 §4).

dig runs the real ``dig`` binary via the subprocess seam (``sandbox = "subprocess"`` — it is NOT
the heavy, container-gated tool) to resolve a domain's records, then maps the resolved addresses
fail-soft. Like every :class:`CliToolConnector` it is argv-safe (a ``list``, the ``--`` flag
terminator) and inherits the SHARED hardened target validator (ADR 0072 §3) + the enforced
``allowed_targets`` allowlist (§2), so a hostile or out-of-list target never reaches the subprocess.

* :meth:`_build_argv` is ``["dig", "+short", "--", target]`` — a list, the ``--`` terminator, the
  validated target a single positional after it;
* :meth:`map` is FAIL-SOFT: a ``dig +short`` body → a thin FtM entity for the queried name carrying
  its resolved addresses (FtM's only IP home is ``UserAccount.ipAddress`` — a deliberately loose v1
  fit; a richer ``wm:`` DNS/host extension is ADR-0072-deferred) + provenance; garbage → ``[]``,
  never raising. The raw record is always landed regardless, so nothing is lost.

The capability is :data:`Capability.ACTIVE`: the cadence driver refuses it; it runs ONLY through the
authorized operator-run path (``runner.operator_run``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from importlib import resources
from typing import Any

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import InvalidEntity, validate_or_raise
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord, Status
from worldmonitor.plugins.cli_tool import CliToolConnector
from worldmonitor.provenance.model import Provenance, stamp

logger = logging.getLogger(__name__)

# A resolved ``dig +short`` answer line that is a bare IPv4 address (one per line). Defensive: the
# tool's stdout is hostile, so we scrape only plainly-shaped addresses rather than a full parser.
_IPV4_LINE_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


class DigConnector(CliToolConnector):
    """ACTIVE dig lookup: validate the target, run ``dig +short -- <target>``, map resolved IPs."""

    sandbox = "subprocess"

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="dig",
            name="dig",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.ACTIVE,
            description="ACTIVE DNS lookup (dig) via an argv-safe, no-shell subprocess.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def _build_argv(self, scope: Mapping[str, Any]) -> list[str]:
        """``["dig", "+short", "--", target]`` — list, ``--`` terminator, the target one element."""
        return ["dig", "+short", "--", str(scope["target"])]

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Fail-soft: a ``dig +short`` body → one thin FtM entity (the queried name + its resolved
        addresses) with provenance; nothing parseable → ``[]``. Never raises.

        DNS↔FtM is a loose fit (no DNS/host schema; ``wm:Host`` is ADR-0072-deferred), so the IPs
        land in ``UserAccount.ipAddress`` — the only FtM IP home — with the queried name as
        ``name``. The raw record is landed by the runner regardless, so an empty map loses nothing.
        """
        text = record.data.decode("utf-8", errors="replace")
        addresses = [
            line.strip() for line in text.splitlines() if _IPV4_LINE_RE.match(line.strip())
        ]
        if not addresses:
            return []

        entity_id = f"dig-host-{hashlib.sha1(record.key.encode('utf-8')).hexdigest()}"
        try:
            entity = validate_or_raise(
                {
                    "id": entity_id,
                    "schema": "UserAccount",
                    "properties": {"name": [record.key], "ipAddress": addresses},
                    "datasets": ["dig"],
                }
            )
        except InvalidEntity:
            logger.debug("dig: un-mappable resolved set for %s; land-only", record.key)
            return []
        return [stamp(entity, provenance)]
