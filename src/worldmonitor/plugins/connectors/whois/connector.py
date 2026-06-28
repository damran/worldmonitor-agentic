"""WhoisConnector — the first ACTIVE CLI-tool connector (ADR 0071 §5/§6).

whois is the active-execution security boundary, so this connector is deliberately rigorous on the
one threat that matters: a hostile scope target must NEVER reach the subprocess as a flag or a shell
command.

* the SHARED, hardened :meth:`CliToolConnector._validate_target` (inherited, ADR 0072 §3) accepts
  only a plain domain / IP (``^[A-Za-z0-9.:-]+$``, no leading ``-``, ``<= 253`` chars, no ``..``),
  rejecting whitespace, shell metachars (``;`` ``$()`` backticks), traversal, newlines, a leading
  dash (a flag), an over-length string, and the empty string;
* :meth:`_build_argv` is EXACTLY ``["whois", "--", target]`` — a list, with the ``--`` flag
  terminator so even a bypassed validation can't turn the target into a flag;
* :meth:`map` is fail-soft: a real whois block → one FtM ``Organization`` (the registrant) with
  provenance; garbage (hostile tool stdout) → ``[]``, never raising.

The capability is :data:`Capability.ACTIVE`: the cadence driver refuses it; it runs ONLY through the
authorized operator-run path (``runner.operator_run``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from importlib import resources
from typing import Any

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import InvalidEntity, validate_or_raise
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord, Status
from worldmonitor.plugins.cli_tool import CliToolConnector
from worldmonitor.provenance.model import Provenance, stamp

# Minimal, fail-soft whois-text parse (the gate is the boundary, not a full whois parser).
_REGISTRANT_RE = re.compile(r"Registrant\s+Organization:\s*(.+)", re.IGNORECASE)
_COUNTRY_RE = re.compile(r"Registrant\s+Country:\s*([A-Za-z]{2})\b", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"Domain\s+Name:\s*(\S+)", re.IGNORECASE)


class WhoisConnector(CliToolConnector):
    """ACTIVE whois lookup: validate the target, run ``whois -- <target>``, map the registrant."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="whois",
            name="whois",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.ACTIVE,
            description="ACTIVE registration lookup (whois) via an argv-safe, no-shell subprocess.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def _build_argv(self, scope: Mapping[str, Any]) -> list[str]:
        """``["whois", "--", target]`` — list form, ``--`` terminator, target one element."""
        return ["whois", "--", str(scope["target"])]

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Fail-soft: a registrant block → one FtM ``Organization`` with provenance; else ``[]``.

        The tool's stdout is hostile bytes — parse defensively, validate via FtM, and skip anything
        unparseable rather than raising (one bad lookup never aborts the run).
        """
        text = record.data.decode("utf-8", errors="replace")
        match = _REGISTRANT_RE.search(text)
        if match is None:
            return []
        registrant = match.group(1).strip()
        if not registrant:
            return []

        properties: dict[str, list[str]] = {"name": [registrant]}
        country = _COUNTRY_RE.search(text)
        if country is not None:
            properties["country"] = [country.group(1).strip()]
        domain_match = _DOMAIN_RE.search(text)
        domain = domain_match.group(1).strip().lower() if domain_match is not None else ""
        if domain:
            # FtM silently drops a non-url value, so this is safe even for an odd domain string.
            properties["website"] = [domain]

        stable = f"{registrant}|{domain or record.key}"
        entity_id = f"whois-org-{hashlib.sha1(stable.encode('utf-8')).hexdigest()}"
        try:
            entity = validate_or_raise(
                {
                    "id": entity_id,
                    "schema": "Organization",
                    "properties": properties,
                    "datasets": ["whois"],
                }
            )
        except InvalidEntity:
            return []
        return [stamp(entity, provenance)]
