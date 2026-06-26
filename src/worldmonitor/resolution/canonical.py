"""Anchor-preferred DURABLE canonical ids + the canonical-alias ledger (ADR 0044).

ADR 0036 conflated two concepts that this module separates. ``resolution/merge.py``'s
``wmc-<sha256(sorted member ids)>`` is a **crash-retry idempotency fingerprint** — it converges on
re-run within ONE membership set, but it is **NOT durable identity**: connectors mint fresh
per-collect member ids on every re-ingest (ADR 0036 §1), so re-ingesting the SAME real entity
yields a DIFFERENT ``wmc-`` id, a different node, and id churn. ADR 0036 §Consequences deferred
re-ingest stability to "Gate B"; this is the front half of that gate.

Durable identity here is **anchor-preferred** — derived from the entity's own canonical
identifiers (Wikidata QID > LEI > registration number > tax number), anchor-kind-prefixed, stable
across re-ingest — with a minted ``wm-mint-<uuid>`` fallback for an unanchored cluster, and a
``canonical_id_ledger`` recording the durable id + its superseded aliases.

ADR 0048 (Gate CID-fix) — the durable id is an **FtM-clean, INJECTIVE** entity reference
``wm-anchor-<kind>-<encoded-value>`` (kind ∈ {qid, lei, regno, taxno}). The earlier
``f"{kind}:{value}"`` serialization (``qid:Q42``) used a **colon**, which is NOT in FtM's
entity-reference charset (``[A-Za-z0-9.-]``): ``registry.entity.clean('qid:Q42') is None``. Because
this durable id is rewritten into edge ENDPOINTS by referent rewriting (``referents.py`` →
``pipeline.py``) and FtM cleans entity-typed values through ``registry.entity``, a colon endpoint
cleaned to ``None`` and the **edge silently dropped** — corrupting the resolved graph for every
anchored entity. ``_anchor_id`` mints an id that is a ``registry.entity.clean`` fixed point AND is
injective over distinct raw values (two provably disjoint namespaces along the trailing
``-<12 hex>`` SHA-256-tail shape; a non-injective id would be a silent cross-entity merge the
catastrophic-merge guard never sees). ``wm-mint-<uuid>`` and the ``wmc-`` fingerprint are unchanged.

DESIGN (spec §3): the durable id is derived OUTSIDE the nomenklatura resolver. nomenklatura already
does anchor-preferred canonical selection but **QID-only** (``Resolver.get_canonical`` returns
``max(connected)`` and only a QID has ``Identifier`` ``weight=3``; LEI/regNo/taxNo are weight-1 raw
ids it would never deterministically prefer), AND the resolver discards its mapping on teardown
(ADR 0028). The richer durable precedence therefore lives here. The resolver decides *membership*;
this module decides the *durable id*. ``pick_anchor`` is pure / DB-free; the ledger helpers take a
SQLAlchemy ``Session``.

ADR 0040 anchor-conflict guard (the gate's #1 person-safety property): if a cluster's members carry
TWO DISTINCT values at the chosen tier (two QIDs, two LEIs, …) the durable id is **NEVER** derived
from that tier — ``pick_anchor`` FALLS THROUGH to the next non-conflicting tier (or to ``None`` →
the caller mints). It MUST NOT silently pick ``[0]``: deriving a durable id is never a back-door
fusion of two real-world identities.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from followthemoney import registry
from rigour.ids.wikidata import is_qid
from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import CanonicalIdLedger
from worldmonitor.ontology.ftm import FtmEntity

# Re-export so callers (and the oracle) use ``canonical.CanonicalIdLedger`` and
# ``CanonicalIdLedger.__table__.create(engine)`` without importing ``db.models`` directly.
__all__ = [
    "CanonicalIdLedger",
    "lookup_durable_for_anchor",
    "mint",
    "pick_anchor",
    "record_alias",
    "record_canonical",
    "record_durable_id",
    "resolve_durable",
    "resolve_durable_id",
]

_MINT_PREFIX = "wm-mint-"

# ADR 0048 — the FtM-clean, INJECTIVE anchor-id serialization. ``_ANCHOR_ID_PREFIX`` namespaces
# every anchor-derived durable id; ``_HASH_TAIL`` is the SHA-256-tail shape (``-<12 hex>``) that
# partitions the clean (verbatim-value) and hashed namespaces — ``[0-9a-f]`` keeps the tail clean.
_ANCHOR_ID_PREFIX = "wm-anchor-"
_HASH_TAIL = re.compile(r"-[0-9a-f]{12}$")


def _anchor_id(kind: str, value: str) -> str:
    """Mint an FtM-clean, INJECTIVE durable id ``wm-anchor-<kind>-<encoded-value>`` (ADR 0048 §3.2).

    Pure / deterministic in ``(kind, raw value)`` — re-ingest stability falls straight out. The id
    is a ``registry.entity.clean`` FIXED POINT for EVERY value (so it survives as a node id AND as a
    rewritten edge endpoint, unlike the old ``f"{kind}:{value}"`` colon form that cleaned to
    ``None`` and dropped the edge), and it is INJECTIVE over distinct raw ``(kind, value)`` pairs (a
    non-injective id would be a silent cross-entity merge the catastrophic-merge guard never sees):

    * **clean (verbatim) branch** — ``wm-anchor-<kind>-<value>`` when the value is already an
      FtM-safe token (QID / LEI / a clean regNo such as ``GOV-9`` always take this branch, staying
      legible); the value is embedded verbatim, so distinct values → distinct ids.
    * **hashed branch** — append ``-<sha256(ORIGINAL value)[:12]>`` iff (a) sanitisation changed the
      value, OR (b) the verbatim candidate is not an FtM fixed point (the CID-5 trailing-``.``/``-``
      / empty class), OR (c) the verbatim candidate would already end in ``-<12 hex>`` (forced into
      the hashed namespace so it can never alias a hostile value's hashed id). Digesting the
      ORIGINAL value keeps sanitisation-collision twins (``HRB/12`` vs ``HRB-12`` both → ``HRB-12``)
      distinct.

    A hashed id ALWAYS ends in ``-<12 hex>``; a clean id NEVER does — the two namespaces are thus
    provably disjoint, and per-kind prefixes keep kinds disjoint. Only hostile regNo/taxNo hash.
    """
    safe = re.sub(r"[^A-Za-z0-9.-]", "-", value)
    candidate = f"{_ANCHOR_ID_PREFIX}{kind}-{safe}"
    if (
        safe != value  # (a) sanitisation changed the value
        or registry.entity.clean(candidate) != candidate  # (b) not an FtM fixed point (CID-5)
        or _HASH_TAIL.search(candidate)  # (c) verbatim id would land in the hashed namespace
    ):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        candidate = f"{_ANCHOR_ID_PREFIX}{kind}-{safe}-{digest}"
    return candidate


# The DURABLE-id precedence (spec §4, first-hit-wins): QID > LEI > regNo > taxNo. This is a
# SEPARATE ordered list from ``ontology.anchors.CANONICAL_ID_FIELDS`` (which has the wrong storage
# keys, no regNo/taxNo, and a place anchor — GeoNames — that is not a legal-identity anchor). Each
# tier names its anchor-kind prefix, the FtM identifier property it reads, and the optional
# ``wm_anchor_*`` context key. ``geonames_id``/``opencorporates_id`` are intentionally NOT in v0
# (no producer; GeoNames is a place anchor) — a later producer extends THIS list, not the Neo4j
# uniqueness constraints.
_ANCHOR_CONTEXT_PREFIX = "wm_anchor_"


@dataclass(frozen=True, slots=True)
class _Tier:
    """One durable-precedence tier (anchor kind + the FtM property + context key it reads)."""

    kind: str
    ftm_prop: str
    context_key: str
    normalize: bool
    valid: Callable[[str], bool]


def _is_lei(value: str) -> bool:
    """A 20-character alphanumeric LEI shape (ISO 17442); ``is_qid`` is False on it."""
    return len(value) == 20 and value.isalnum()


# QID > LEI > regNo > taxNo. regNo/taxNo are normalized via the FtM ``identifier`` type exactly as
# ADR 0039's ``_distinguishing_ids`` does, so the same government id reconciles whether it was
# stored as ``registrationNumber`` on one record and ``taxNumber`` on another.
_PRECEDENCE: tuple[_Tier, ...] = (
    _Tier("qid", "wikidataId", "wikidata_id", normalize=False, valid=is_qid),
    _Tier("lei", "leiCode", "lei", normalize=False, valid=_is_lei),
    _Tier("regno", "registrationNumber", "registration_number", normalize=True, valid=bool),
    _Tier("taxno", "taxNumber", "tax_number", normalize=True, valid=bool),
)


def _context_values(entity: FtmEntity, key: str) -> list[str]:
    """Distinct string values held at ``wm_anchor_<key>`` in the entity context (list or scalar)."""
    raw = entity.context.get(f"{_ANCHOR_CONTEXT_PREFIX}{key}")
    if raw is None:
        return []
    candidates = raw if isinstance(raw, list) else [raw]
    return [c for c in candidates if isinstance(c, str) and c]


def _tier_values(entity: FtmEntity, tier: _Tier) -> set[str]:
    """The VALID, normalized anchor values one entity carries at ``tier``.

    Reads the FtM identifier property AND the ``wm_anchor_*`` context, normalizing regNo/taxNo via
    the FtM ``identifier`` type (ADR 0039) and keeping only values that pass the tier's validity
    check (``is_qid`` for QID, the 20-char shape for LEI, non-empty for regNo/taxNo).
    """
    raw: list[str] = []
    raw.extend(value for value in entity.get(tier.ftm_prop, quiet=True) if value)
    raw.extend(_context_values(entity, tier.context_key))
    values: set[str] = set()
    for value in raw:
        candidate = registry.identifier.clean(value) if tier.normalize else value
        if candidate and tier.valid(candidate):
            values.add(candidate)
    return values


def pick_anchor(members: Sequence[FtmEntity]) -> str | None:
    """Return the anchor-preferred DURABLE id over ``members``, or ``None`` if none is usable.

    Honors the precedence QID > LEI > regNo > taxNo, serialized by ``_anchor_id`` as the FtM-clean,
    injective ``wm-anchor-<kind>-<encoded-value>`` (ADR 0048: ``wm-anchor-qid-Q42`` /
    ``wm-anchor-lei-<20-char>`` / ``wm-anchor-regno-<…>`` / ``wm-anchor-taxno-<…>``), reading each
    tier from the FtM identifier property (``wikidataId`` / ``leiCode`` / ``registrationNumber`` /
    ``taxNumber``) and/or the ``wm_anchor_*`` context. DB-free and pure (unit-testable like
    ``cluster_and_merge``).

    ADR 0040 anchor-conflict guard: if the cluster's members carry TWO DISTINCT values at a tier,
    that tier is in conflict and is SKIPPED — ``pick_anchor`` falls through to the next
    non-conflicting tier (NEVER picks ``[0]``; a durable id is never a back-door fusion of two
    real-world identities). Returns ``None`` (→ the caller mints) if every tier is empty or in
    conflict.
    """
    for tier in _PRECEDENCE:
        union: set[str] = set()
        for member in members:
            union |= _tier_values(member, tier)
        if len(union) == 1:
            return _anchor_id(tier.kind, next(iter(union)))
        # 0 values → tier empty, try the next; >1 distinct values → ADR-0040 conflict at this
        # tier, FALL THROUGH (never pick an arbitrary winner from a conflicting anchor set).
    return None


def mint() -> str:
    """Mint a durable id for an unanchored cluster with no prior ledger entry.

    Shape ``wm-mint-<uuid>`` — distinct from the ``wmc-`` idempotency fingerprint and from the
    anchor-prefixed forms (``wm-anchor-<kind>-…`` for kind ∈ {qid, lei, regno, taxno}).
    """
    return f"{_MINT_PREFIX}{uuid.uuid4()}"


def _row_id(canonical_id: str, alias: str) -> str:
    """Deterministic primary key for a (canonical, alias) row — makes inserts idempotent.

    The PK is a UUID5 over the (canonical, alias) pair so a re-record of the same pair re-derives
    the SAME id (the second write is a no-op against the unique (canonical, alias) constraint, but
    a stable PK keeps the no-op robust even where the constraint check races).
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{canonical_id}\x00{alias}").hex


def _alias_exists(session: Session, canonical_id: str, alias: str) -> bool:
    """True if a (canonical_id, alias) row already exists (the idempotency check)."""
    stmt = select(CanonicalIdLedger.id).where(
        CanonicalIdLedger.canonical_id == canonical_id,
        CanonicalIdLedger.canonical_alias == alias,
    )
    return session.execute(stmt).first() is not None


def record_canonical(
    session: Session, canonical_id: str, *, anchor_kind: str = "", anchor_value: str = ""
) -> None:
    """Record a durable canonical id (idempotent: a second call for the same id is a no-op).

    Writes the canonical SELF-row (``canonical_alias == canonical_id``) carrying the anchor
    kind/value. A re-derive of the same anchored input must not duplicate the row (MR-5), so the
    write is skipped if the self-row already exists.
    """
    if _alias_exists(session, canonical_id, canonical_id):
        return
    session.add(
        CanonicalIdLedger(
            id=_row_id(canonical_id, canonical_id),
            canonical_id=canonical_id,
            canonical_alias=canonical_id,
            anchor_kind=anchor_kind,
            anchor_value=anchor_value,
        )
    )
    session.flush()


def record_alias(session: Session, canonical_id: str, alias: str) -> None:
    """Record one APPEND-ONLY alias row mapping ``alias`` → ``canonical_id``.

    Idempotent per ``(canonical_id, alias)``: a duplicate is a no-op (no second row). A split's
    ejected id is recorded here as a traceable alias — append-only, the un-merge never deletes.
    """
    if _alias_exists(session, canonical_id, alias):
        return
    session.add(
        CanonicalIdLedger(
            id=_row_id(canonical_id, alias),
            canonical_id=canonical_id,
            canonical_alias=alias,
        )
    )
    session.flush()


def resolve_durable(session: Session, alias: str) -> str | None:
    """The surviving durable id a (superseded) ``alias`` resolves to, else ``None``.

    A canonical self-row makes a durable id resolve to itself; an alias row redirects a superseded
    id onto its survivor — the durable mirror of nomenklatura's ``get_referents`` (superseded-id
    traceability), so no edge dangles at a merged-away id.
    """
    stmt = select(CanonicalIdLedger.canonical_id).where(CanonicalIdLedger.canonical_alias == alias)
    return session.execute(stmt).scalars().first()


def lookup_durable_for_anchor(session: Session, anchor_id: str) -> str | None:
    """The durable id already recorded for ``anchor_id`` (the ADOPT read), else ``None``.

    ``anchor_id`` is an anchor-kind-prefixed durable id (``pick_anchor``'s output, e.g.
    ``wm-anchor-qid-Q42``). A re-ingested anchored member adopts this existing durable id instead of
    minting a new one — no id churn, no second node (spec §7 adopt).
    """
    stmt = select(CanonicalIdLedger.canonical_id).where(
        CanonicalIdLedger.canonical_id == anchor_id,
        CanonicalIdLedger.canonical_alias == anchor_id,
    )
    return session.execute(stmt).scalars().first()


def resolve_durable_id(
    session: Session,
    members: Sequence[FtmEntity],
    *,
    fallback_id: str,
) -> str:
    """Compute (READ-ONLY) the durable canonical id for a cluster — NO ledger write (spec §7).

    The adopt-preferring read the pipeline runs to know a cluster's durable id BEFORE deciding to
    promote it (a parked cluster must not write to the ledger):

    1. ``pick_anchor(members)`` — the anchor-preferred durable id (honoring the ADR-0040 conflict
       guard). If it exists, ADOPT an existing durable id recorded for that anchor (no churn, no
       second node — A3), else the anchor id itself (first sighting — A2).
    2. else (no usable anchor) the ``fallback_id`` — the cluster's ``wmc-`` idempotency fingerprint
       (a real merge) or the singleton's own id. A DURABLE id is derived FROM a ``wmc-`` hash in NO
       path (DENY D1): the fingerprint is reused as-is, never re-hashed into a fresh durable id.

    Pair with :func:`record_durable_id` at the PROMOTE point to write the ledger entry + aliases.
    """
    anchor = pick_anchor(members)
    if anchor is None:
        return fallback_id
    existing = lookup_durable_for_anchor(session, anchor)
    return existing if existing is not None else anchor


def record_durable_id(
    session: Session,
    durable_id: str,
    *,
    member_ids: Sequence[str],
    prior_id: str | None = None,
) -> None:
    """Record a PROMOTED cluster's durable id + its collapsed-member aliases (spec §6/§7).

    Writes (all idempotent / append-only):
    * the canonical SELF-row for ``durable_id`` (anchor kind/value parsed from its prefix; ``mint``
      for a ``wm-mint-`` id; empty for the unanchored ``wmc-``/singleton fallback);
    * one alias row per collapsed member id (A4 — every collapsed id traces to the survivor);
    * an alias row for ``prior_id`` (the cluster's prior ``wmc-`` fingerprint) when the durable id
      differs from it, so a lookup by the old fingerprint still resolves to the surviving node.

    A singleton keyed under its OWN id records only the self-row (member == durable). No durable id
    is EVER derived from a ``wmc-`` hash — ``wmc-`` only ever appears here as the fallback value of
    ``durable_id`` itself (an unanchored merge) or as a recorded ``prior_id`` alias.
    """
    if durable_id.startswith(_ANCHOR_ID_PREFIX):
        # ADR 0048: the colon discriminator is gone — the anchor kind is the first token after the
        # ``wm-anchor-`` prefix (kinds never contain a hyphen), the encoded value is the remainder.
        kind, _, value = durable_id.removeprefix(_ANCHOR_ID_PREFIX).partition("-")
        record_canonical(session, durable_id, anchor_kind=kind, anchor_value=value)
    elif durable_id.startswith(_MINT_PREFIX):
        record_canonical(session, durable_id, anchor_kind="mint", anchor_value="")
    else:
        record_canonical(session, durable_id, anchor_kind="", anchor_value="")
    for member_id in member_ids:
        if member_id != durable_id:
            record_alias(session, durable_id, member_id)
    if prior_id is not None and prior_id != durable_id:
        record_alias(session, durable_id, prior_id)
