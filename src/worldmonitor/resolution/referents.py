"""Referent rewriting — redirect merged-away source ids to their canonical id.

When entity resolution collapses several source entities into one canonical
entity, any *other* entity that referenced a collapsed source by id — an edge
endpoint (``Ownership.owner``, ``Directorship.director``) or any entity-typed
property — still names the dead source id. Written as-is, that reference creates
a relationship to a node that was never materialised: a dangling edge to a bare,
provenance-less id. This module rewrites those references to the surviving
canonical id, upholding the non-negotiable "resolve to canonical IDs" invariant
for edges as well as nodes.

Only clusters that are actually **promoted** (written to the graph) contribute a
mapping. A block-mode parked cluster (ADR 0024) is never written, so its members
keep their own ids and references to them are left untouched — exactly the
"block-mode parked merges do not rewrite" rule. Alert-mode flagged merges *are*
promoted, so they do rewrite.

Rewriting only ever changes entity-typed *property values*; an entity's
provenance/context and all other properties are untouched, so an edge keeps the
provenance of the assertion that created it (G1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from followthemoney import registry

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.resolution.merge import ResolvedCluster


def build_referent_map(clusters: Iterable[ResolvedCluster]) -> dict[str, str]:
    """Map every member id to its cluster's canonical id.

    Pass only **promoted** clusters (those written to the graph): a parked
    cluster must not redirect anything. Singletons map to themselves (a no-op);
    a real merge maps each collapsed source id to the surviving canonical id.

    The pipeline re-keys each promoted cluster under its anchor-preferred DURABLE id
    (Gate B-front / ADR 0044) before building this map, so an edge endpoint naming a
    merged-away source id is redirected onto the stable durable id (``qid:``/``lei:``/…
    /``wm-mint-``), not the ``wmc-`` idempotency fingerprint — references stay valid
    across re-ingest.
    """
    referents: dict[str, str] = {}
    for cluster in clusters:
        for member_id in cluster.member_ids:
            referents[member_id] = cluster.canonical_id
    return referents


def rewrite_referents(entity: FtmEntity, referents: Mapping[str, str]) -> FtmEntity:
    """Rewrite ``entity``'s entity-typed property values through ``referents``.

    Every value of an entity-typed property (edge endpoints and entity
    references) that names a merged-away id is replaced by its canonical id; ids
    absent from the map — singletons, parked members, out-of-batch references —
    are left unchanged. Non-entity properties and the entity's provenance/context
    are never touched. Mutates ``entity`` in place and returns it.
    """
    if not referents:
        return entity
    for prop in entity.schema.properties.values():
        if prop.type != registry.entity:
            continue
        values = entity.get(prop, quiet=True)
        if not values:
            continue
        rewritten = [referents.get(value, value) for value in values]
        if rewritten != values:
            entity.set(prop, rewritten)
    return entity
