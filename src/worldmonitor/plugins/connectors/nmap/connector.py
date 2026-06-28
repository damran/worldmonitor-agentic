"""NmapConnector — the heavy, container-gated ACTIVE CLI-tool connector (ADR 0072 §5).

nmap is an un-sandboxed network scanner; running it from the host violates "heavy CLI tools in
containers", so it declares ``sandbox = "container"`` and its EXECUTION is REFUSED by the
operator-run gate (``run_connector_once`` raises ``SandboxUnavailableError`` -> REST 409) until a
real container sandbox is enabled (``settings.container_sandbox_enabled``, default False in v1). The
connector is otherwise fully BUILT (manifest, schema, argv, map) and tested — when the Stage-4
sandbox lands and the flag flips, it runs in it with NO connector change.

* :meth:`_build_argv` is ``["nmap", "-oX", "-", "--", target]`` — a list (XML to stdout), the ``--``
  terminator, the validated target a single positional after it (argv-safe, no shell);
* :meth:`map` is FAIL-SOFT: an ``nmap -oX -`` body → a thin FtM entity for the scanned host carrying
  its address (FtM's only IP home is ``UserAccount.ipAddress`` — a deliberately loose v1 fit; a
  ``wm:`` host/service extension is ADR-0072-deferred) + provenance; garbage → ``[]``, never
  raising. The hostile XML is scraped with a bounded regex (no XML parser → no XXE/entity surface).

The capability is :data:`Capability.ACTIVE`: the cadence driver refuses it; only the operator-run
path could run it — and that path refuses it in v1 (the heavy-tool sandbox gate).
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

# Scrape an ``addr="1.2.3.4"`` IPv4 host address out of nmap's XML defensively — the tool's stdout
# is hostile, so we regex the plainly-shaped address rather than feed it to an XML parser (no XXE /
# no entity-expansion surface; no XML dependency).
_XML_IPV4_ADDR_RE = re.compile(r'addr="((?:\d{1,3}\.){3}\d{1,3})"')


class NmapConnector(CliToolConnector):
    """ACTIVE nmap scan: argv-safe + container-gated (execution refused until the sandbox lands)."""

    sandbox = "container"

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="nmap",
            name="nmap",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.ACTIVE,
            description=(
                "ACTIVE network scan (nmap) — argv-safe; EXECUTION container-gated (ADR 0072)."
            ),
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def _build_argv(self, scope: Mapping[str, Any]) -> list[str]:
        """``["nmap", "-oX", "-", "--", target]`` — list form, XML to stdout, ``--`` terminator,
        the validated target one element after it (an argv-safe, no-shell command)."""
        return ["nmap", "-oX", "-", "--", str(scope["target"])]

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Fail-soft: an ``nmap -oX -`` body → one thin FtM entity (the scanned host + its address)
        with provenance; nothing parseable → ``[]``. Never raises.

        Host↔FtM is a loose fit (no host/service schema; ``wm:Host`` is ADR-0072-deferred), so the
        host address lands in ``UserAccount.ipAddress`` — the only FtM IP home — with the scanned
        name as ``name``. The raw record is landed by the runner regardless.
        """
        text = record.data.decode("utf-8", errors="replace")
        addresses = _XML_IPV4_ADDR_RE.findall(text)
        if not addresses:
            return []

        # De-dup while preserving order (a host can appear once per protocol in the XML).
        unique = list(dict.fromkeys(addresses))
        entity_id = f"nmap-host-{hashlib.sha1(record.key.encode('utf-8')).hexdigest()}"
        try:
            entity = validate_or_raise(
                {
                    "id": entity_id,
                    "schema": "UserAccount",
                    "properties": {"name": [record.key], "ipAddress": unique},
                    "datasets": ["nmap"],
                }
            )
        except InvalidEntity:
            logger.debug("nmap: un-mappable scan result for %s; land-only", record.key)
            return []
        return [stamp(entity, provenance)]
