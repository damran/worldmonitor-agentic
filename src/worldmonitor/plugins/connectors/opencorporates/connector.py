"""OpenCorporates connector — company registry data anchored on opencorporates_id.

OpenCorporates exposes a paginated JSON search API
(``GET https://api.opencorporates.com/v0.4/companies/search``) returning companies wrapped in a
``{"results": {"companies": [{"company": {...}}], "total_pages": N}}`` envelope. Each company maps
to an FtM ``Company`` carrying the ``opencorporates_id`` canonical anchor
(``"{jurisdiction_code}/{company_number}"``). A passive ``EXTERNAL_IMPORT`` connector subclassing
:class:`RestApiConnector` — it never writes to the graph; raw lands in the landing zone and
candidates go to the ER queue. The ``api_token`` is a config secret and is never logged.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterable, Mapping
from importlib import resources
from typing import Any

from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord, Status
from worldmonitor.plugins.rest_api import RestApiConnector
from worldmonitor.provenance.model import Provenance, stamp

_SEARCH_URL = "https://api.opencorporates.com/v0.4/companies/search"
_DEFAULT_PER_PAGE = 30

# OpenCorporates company field -> FtM Company property (only set when the source value is present).
_PROPERTY_MAP = {
    "name": "name",
    "company_number": "registrationNumber",
    "jurisdiction_code": "jurisdiction",
    "incorporation_date": "incorporationDate",
    "dissolution_date": "dissolutionDate",
    "company_type": "legalForm",
    "current_status": "status",
    "registered_address_in_full": "address",
    "opencorporates_url": "sourceUrl",
}


class OpenCorporatesConnector(RestApiConnector):
    """Imports OpenCorporates companies (FtM Company) anchored on ``opencorporates_id``."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="opencorporates",
            name="OpenCorporates",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description="Company registry data; anchors companies to OpenCorporates IDs.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def _page_url(self, config: Mapping[str, Any], page: int) -> str:
        """Build the companies-search URL for ``page`` (q + per_page + page + api_token + filter).

        The optional ``jurisdiction_code`` is included only when set; ``q`` / ``api_token`` are
        required by the schema. The api_token rides the query string — never log this URL.
        """
        params: dict[str, Any] = {
            "q": config["q"],
            "per_page": int(config.get("per_page", _DEFAULT_PER_PAGE)),
            "page": page,
            "api_token": config["api_token"],
        }
        jurisdiction = config.get("jurisdiction_code")
        if jurisdiction:
            params["jurisdiction_code"] = jurisdiction
        return f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"

    def _extract_items(self, payload: Any) -> list[dict[str, Any]]:
        """Unwrap ``results.companies[].company`` (tolerate a missing/empty envelope -> [])."""
        try:
            companies: Any = payload["results"]["companies"]
            return [entry["company"] for entry in companies]
        except (KeyError, TypeError):
            return []

    def _total_pages(self, payload: Any) -> int:
        """Total page count from the envelope (``results.total_pages``; default 1 if absent)."""
        try:
            return int(payload["results"]["total_pages"])
        except (KeyError, TypeError, ValueError):
            return 1

    def _record_key(self, item: Mapping[str, Any]) -> str:
        """Stable per-company key: ``"{jurisdiction_code}/{company_number}"``."""
        return f"{item.get('jurisdiction_code', '')}/{item.get('company_number', '')}"

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Map one OpenCorporates company to an FtM Company with its anchor + provenance.

        A record missing a ``company_number`` or ``name`` is identity-less and dropped (``[]``,
        fail-soft on a single row) rather than raising and failing the batch.
        """
        company = json.loads(record.data)
        company_number = str(company.get("company_number") or "").strip()
        name = str(company.get("name") or "").strip()
        jurisdiction = str(company.get("jurisdiction_code") or "").strip()
        if not company_number or not name:
            return []

        properties: dict[str, list[str]] = {}
        for source_field, ftm_property in _PROPERTY_MAP.items():
            value = company.get(source_field)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                properties[ftm_property] = [text]

        entity = validate_or_raise(
            {
                "id": f"opencorporates-{jurisdiction}-{company_number}",
                "schema": "Company",
                "properties": properties,
                "datasets": ["opencorporates"],
            }
        )
        set_anchor(entity, "opencorporates_id", f"{jurisdiction}/{company_number}")
        return [stamp(entity, provenance)]
