"""``GET /sources/freshness`` — the derived source-freshness REST surface (Gate F-1, ADR 0123).

Auth-gated (``get_principal``, mirroring ``api/graph.py``) + DB-backed (``get_db`` ->
``app.state.db_sessions``). Read-only; no path params; no injection surface. Postgres-only —
freshness lives entirely in ``ConnectorInstance`` + ``task_run`` (spec §3.6), so this route never
touches Neo4j. A thin consumer of the ONE shared
:func:`worldmonitor.observability.freshness.compute_instance_freshness` helper (ADR 0123 D2); it
never re-derives state — the driver's Prometheus gauge is the other consumer, and a parity test
pins that the two report the same state per instance.

Opaque ids only (INV-7, spec §3.3): the response carries only ``instance_id`` (server-minted uuid)
+ ``connector_id`` — never connector config/secret/URL/dataset/person field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from worldmonitor.api.deps import get_db, get_principal
from worldmonitor.authz.oidc import Principal
from worldmonitor.observability.freshness import FRESHNESS_STATES, compute_instance_freshness

router = APIRouter(tags=["freshness"])


@router.get("/sources/freshness")
def read_sources_freshness(
    request: Request,
    _principal: Annotated[Principal, Depends(get_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Return the derived 6-state freshness of every connector instance (spec §4.2).

    ``generated_at`` is the snapshot wall-clock — freshness is inherently a function of ``now``,
    so this is honest, not a determinism break (unlike F-3's dossier, there is no byte-parity
    requirement on this surface).
    """
    settings = request.app.state.settings
    now = datetime.now(UTC)
    rows = compute_instance_freshness(
        db,
        now=now,
        stale_after_seconds=settings.freshness_stale_after_seconds,
        very_stale_after_seconds=settings.freshness_very_stale_after_seconds,
    )

    summary: dict[str, int] = dict.fromkeys(FRESHNESS_STATES, 0)
    sources: list[dict[str, Any]] = []
    for row in rows:
        summary[row.freshness_status] += 1
        sources.append(
            {
                "instance_id": row.instance_id,
                "connector_id": row.connector_id,
                "status": row.status,
                "freshness_status": row.freshness_status,
                "last_run": row.last_run.isoformat() if row.last_run is not None else None,
                "last_success_at": (
                    row.last_success_at.isoformat() if row.last_success_at is not None else None
                ),
                "age_seconds": row.age_seconds,
            }
        )
    summary["total"] = len(rows)

    return {
        "generated_at": now.isoformat(),
        "budget": {
            "stale_after_seconds": settings.freshness_stale_after_seconds,
            "very_stale_after_seconds": settings.freshness_very_stale_after_seconds,
        },
        "summary": summary,
        "sources": sources,
    }
