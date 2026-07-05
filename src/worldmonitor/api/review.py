"""Review-queue web UI — the read-only surface (Gate 1a, ADR 0103).

Promotes the sign-off CLI's ``list`` view (``python -m worldmonitor.review``,
:mod:`worldmonitor.resolution.signoff`) to a server-rendered HTMX surface: ``GET /review``
(the parked ``pending_review`` queue — counts, the base "blocked pending human sign-off"
state, a prominent sensitive badge, a confidence band, the guard reason verbatim) and
``GET /review/card`` (an HTMX fragment: side-by-side member cards built from
``ErQueueItem.raw_entity`` + a statement-level evidence diff).

**This slice writes nothing** (ADR 0103 Decision A — 1a is person-neutral). Every route is
``get_principal``-gated. A parked merge has NO ``StatementRecord`` rows (the Gate 2a dual-write
fires only on the promoted ``"merged"`` path) so the evidence diff is built EXCLUSIVELY from
``ErQueueItem.raw_entity`` (spec §2.1). The member loader below is a deliberate, read-only
sibling of ``signoff._member_rows``/``signoff._outbound_edges`` — it must NOT call those (they
``session.add`` a dead-letter row on a poison ``raw_entity``, i.e. they write); an unparseable
member instead degrades to an "unparseable" card and nothing is persisted.

The sensitive badge is driven by ``guard.sensitivity.is_sensitive`` over the REAL parsed
members — never by substring-matching ``merge_audit.reason`` (the guard's own code warns the
reason embeds hostile data-bearing fields, e.g. member ids / anchor values).
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from worldmonitor.api.deps import get_db, get_neo4j, get_principal
from worldmonitor.authz.oidc import Principal
from worldmonitor.db.models import ErQueueItem, MergeAudit
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import get_provenance
from worldmonitor.resolution import signoff
from worldmonitor.resolution.review import is_sensitive

router = APIRouter(tags=["review"])


# ------------------------------------------------------------------------------------------------
# CSRF — mint-only in 1a (``_check_csrf`` arrives in 1b alongside ``POST /review/verdict``).
# ------------------------------------------------------------------------------------------------
def _csrf_token(request: Request) -> str:
    """Return the session CSRF token, minting one on first read (mint-on-form-GET).

    Deliberate ~6-line duplication of ``integrations._csrf_token`` (ADR 0103 Decision E):
    ``api/integrations.py`` is frozen, so this router carries its own small copy rather than
    editing it. 1a mints the token into both contexts so 1b's verdict form only has to add
    itself.
    """
    token: str = request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return token


# ------------------------------------------------------------------------------------------------
# Read-only member loading (NEVER call signoff._member_rows / _outbound_edges — they write).
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _MemberView:
    """A read-only render view of one parked cluster member (an ``ErQueueItem`` row)."""

    member_id: str
    schema: str | None
    parseable: bool
    sensitive: bool
    props: dict[str, list[str]]
    source_id: str | None
    reliability: str | None
    retrieved_at: str | None
    raw_pointer: str


def _pending_members_by_id(db: Session) -> dict[str, ErQueueItem]:
    """A read-only index of every queued ``pending_review`` row, keyed by its ``raw_entity`` id.

    Mirrors ``signoff._member_rows``'s row selection (``status == 'pending_review'``) WITHOUT
    its dead-lettering: a row whose ``raw_entity`` cannot even yield an ``"id"`` key is simply
    skipped from the index (nothing is written here; a poison row's own schema is validated
    later, per-member, in :func:`_member_view`, which degrades it to "unparseable" rather than
    raising).
    """
    rows = db.execute(select(ErQueueItem).where(ErQueueItem.status == "pending_review")).scalars()
    index: dict[str, ErQueueItem] = {}
    for row in rows:
        raw = row.raw_entity
        # The JSON column is TYPED ``dict`` but a malformed DB row could hold a non-dict at
        # runtime — this guard is a real defence, not the tautology pyright infers from the type.
        if not isinstance(raw, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            continue
        row_id = raw.get("id")
        if isinstance(row_id, str):
            index[row_id] = row
    return index


def _member_view(row: ErQueueItem) -> _MemberView:
    """Build a render view of ``row``: parse via ``make_entity``, degrading on failure.

    An unparseable ``raw_entity`` (unknown schema / malformed shape) renders as an "unparseable"
    card — no exception escapes, and nothing is written (contrast ``signoff._member_rows``, which
    dead-letters the same case). Its sensitivity is **FAIL-CLOSED**: a member we cannot parse
    cannot be *cleared* as non-sensitive, so on this human merge-review surface it counts as
    sensitive (the badge is a caution, never a verdict). A sensitivity warning must fail toward
    MORE caution, never less — a sanction-tagged member that happens not to parse must not render
    silently un-flagged. (Unreachable via the supported pipeline, which quarantines unparseable
    entities before clustering — this is defence-in-depth against that invariant ever eroding.)
    """
    # Runtime defence: a malformed DB row could hold a non-dict despite the typed JSON column.
    raw = row.raw_entity if isinstance(row.raw_entity, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
    entity: FtmEntity | None
    try:
        entity = make_entity(raw)
    except Exception:
        entity = None

    if entity is not None:
        provenance = get_provenance(entity)
        member_id = entity.id or raw.get("id") or row.id
        return _MemberView(
            member_id=member_id,
            schema=entity.schema.name,
            parseable=True,
            sensitive=is_sensitive(entity),
            props={name: list(values) for name, values in entity.properties.items()},
            source_id=provenance.source_id if provenance is not None else row.connector_id,
            reliability=provenance.reliability if provenance is not None else None,
            retrieved_at=provenance.retrieved_at if provenance is not None else None,
            raw_pointer=row.source_record,
        )

    fallback_schema = raw.get("schema")
    fallback_id = raw.get("id")
    return _MemberView(
        member_id=(fallback_id if isinstance(fallback_id, str) else None)
        or row.entity_id
        or row.id,
        schema=fallback_schema if isinstance(fallback_schema, str) else None,
        parseable=False,
        sensitive=True,  # FAIL-CLOSED: unparseable ⇒ cannot be cleared ⇒ show the caution badge
        props={},
        source_id=row.connector_id,
        reliability=None,
        retrieved_at=None,
        raw_pointer=row.source_record,
    )


def _load_members(db: Session, source_ids: Sequence[str]) -> list[_MemberView]:
    """Read-only: every member row of ``source_ids`` found in the parked queue, as render views."""
    index = _pending_members_by_id(db)
    return [_member_view(index[member_id]) for member_id in source_ids if member_id in index]


def _evidence_diff(members: Sequence[_MemberView]) -> list[dict[str, Any]]:
    """Build the statement-level evidence diff: per FtM property, each member's value(s).

    The union of property names is taken over PARSEABLE members only (an unparseable member
    contributes no properties to the diff, but still renders its own degraded card). A property
    is marked "agree" when every member that carries a value for it carries the SAME value(s);
    otherwise it is a "contradict".
    """
    parseable = [member for member in members if member.parseable]
    prop_names = sorted({name for member in parseable for name in member.props})
    diff: list[dict[str, Any]] = []
    for name in prop_names:
        values_by_member = {member.member_id: member.props.get(name, []) for member in parseable}
        present = {tuple(values) for values in values_by_member.values() if values}
        # NOTE: the dict key is "member_values", never "values" — a plain dict's ``.values``
        # is a bound built-in method, so Jinja's attribute-then-item lookup (``entry.values``)
        # would silently resolve to that method instead of this key (AttributeError-free, so
        # it is NOT caught by ``|default`` — it fails loudly deep in template rendering).
        diff.append({"prop": name, "member_values": values_by_member, "agree": len(present) <= 1})
    return diff


# ------------------------------------------------------------------------------------------------
# Routes (all behind get_principal). No POST, no write, in this slice (ADR 0103 Decision A).
# ------------------------------------------------------------------------------------------------
@router.get("/review", include_in_schema=False)
def review_queue(
    request: Request,
    _principal: Annotated[Principal, Depends(get_principal)],
    db: Annotated[Session, Depends(get_db)],
    neo4j: Annotated[Neo4jClient, Depends(get_neo4j)],
) -> Response:
    """Render every parked (``pending_review``) merge — counts, badges, band, reason."""
    parked = signoff.list_parked(db, neo4j)
    rows: list[dict[str, Any]] = []
    for merge in parked:
        members = _load_members(db, merge.source_ids)
        rows.append(
            {
                "canonical_id": merge.canonical_id,
                "member_count": len(merge.source_ids),
                "score": merge.score,
                "reason": merge.reason,
                "sensitive": any(member.sensitive for member in members),
                "graph_written": merge.graph_written,
            }
        )
    context = {"rows": rows, "csrf_token": _csrf_token(request)}
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "review.html", context)


@router.get("/review/card", include_in_schema=False)
def review_card(
    request: Request,
    canonical_id: str,
    _principal: Annotated[Principal, Depends(get_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Render an HTMX fragment: side-by-side member cards + the evidence diff.

    ``canonical_id`` is a QUERY param (ids can contain ``:``, e.g. ``qid:Q42``). 404s unless it
    names a CURRENT ``pending_review`` audit — a completed (merged/rejected) or unknown id never
    renders a card.
    """
    audit = (
        db.execute(
            select(MergeAudit).where(
                MergeAudit.canonical_id == canonical_id,
                MergeAudit.decision == "pending_review",
            )
        )
        .scalars()
        .first()
    )
    if audit is None:
        raise HTTPException(status_code=404, detail="No parked (pending_review) merge with that id")

    members = _load_members(db, list(audit.source_ids))
    context = {
        "canonical_id": canonical_id,
        "score": audit.score,
        "reason": audit.reason,
        "sensitive": any(member.sensitive for member in members),
        "members": members,
        "diff": _evidence_diff(members),
        "csrf_token": _csrf_token(request),
    }
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "review_card.html", context)
