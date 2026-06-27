"""Property: node provenance integrity — additive re-emit + fail-closed node (ADR 0060).

Two node-provenance invariants the writer's Pass 1 owns (the node analogue of the edge
G1 fix, ADR 0055):

1. **Additive node write.** The node generator must emit ``SET n += props`` (additive),
   NEVER ``SET n = props`` (full replace). Under ``SET n = props`` a *thinner* re-emit of
   the same ``{id}`` (a sparser source variant, or a B-1 re-resolve) silently ERASES the
   node's prior anchors / ``prov_*`` / ``prov_witnesses`` — a G1 (provenance-on-every-node)
   + anchor-stability regression on any re-ingest. We assert the property on the generated
   ``QueryBatch.query`` string so it is a pure, deterministic decision (no Neo4j): the
   fork's :func:`generate_node_entity` is additive for ANY entity shape.

2. **Fail-closed node provenance.** A non-edge entity reaching Pass 1 with NO provenance
   must halt the write with ``NodeProvenanceError`` (a ``ValueError``, importable from
   ``worldmonitor.graph.writer``, mirroring ``EdgeProvenanceError`` from ADR 0055) rather
   than write a node with no ``prov_*``. The pure decision the writer encodes is
   ``bool(provenance_node_properties(entity))`` (the predicate the edge path uses): empty
   ⇒ would-raise. We assert that contract — an unstamped non-edge entity has an EMPTY
   provenance projection (the "no provenance" signal) while a stamped one is non-empty and
   carries ``prov_source_id`` — together with the existence of ``NodeProvenanceError``.

(The live round-trip — that ``write_entities`` preserves anchors on re-emit, raises on an
unstamped node, and keeps ghost endpoints exempt — is the integration suite in
``tests/integration/test_graph_writer.py`` (real Neo4j). These properties pin the pure
decisions.)
"""

from __future__ import annotations

from pathlib import Path

import strategies as wm
from ftmg.config import Configuration, DatabaseConfig
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.graph.ftmg_fork import generate_node_entity
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.provenance.model import PROVENANCE_NODE_PREFIX, provenance_node_properties

_SETTINGS = settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])


def _config() -> Configuration:
    """An ftmg :class:`Configuration` with dummy creds — node-query GENERATION needs no live DB."""
    return Configuration(
        path=Path("."),
        db=DatabaseConfig(url="bolt://unused:7687", username="u", password="p"),  # noqa: S106
    )


@given(entity=st.one_of(wm.ftm_entity(), wm.source_tagged_entity()))
@_SETTINGS
def test_node_generator_is_additive_never_clobbers(entity: FtmEntity) -> None:
    """The fork's ``generate_node_entity`` emits ``SET n += props``, NEVER ``SET n = props``.

    ADR 0060 defect 1: a full-replace ``SET n = props`` wipes prior anchors / ``prov_*`` /
    ``prov_witnesses`` on a thinner re-emit. The fork must override the node generator to
    accumulate. Asserted on the generated query for EVERY entity shape, so the builder
    cannot leave the node path on the upstream clobbering generator.

    RED today: ``ftmg_fork`` re-exports upstream ``generate_node_entity`` unchanged, whose
    query is ``MERGE (n {{id: props.id}}) SET n = props`` — the additive assertion fails.
    """
    config = _config()
    batches = list(generate_node_entity(config, entity))
    assert batches, "a non-edge entity must yield at least one node-creation batch"

    # The node-creation batch is the one that MERGEs the node and SETs its props.
    node_batches = [b for b in batches if "MERGE" in b.query and "SET n" in b.query]
    assert node_batches, "expected a MERGE (n ...) SET n ... node-creation batch"

    for batch in node_batches:
        query = batch.query
        # Additive write — accumulate, never replace.
        assert "SET n += props" in query, (
            "node write must be additive (`SET n += props`) so a thinner re-emit cannot "
            f"clobber prior anchors / prov_* / prov_witnesses (ADR 0060 defect 1); got: {query!r}"
        )
        # And explicitly NOT the upstream full-replace form (the M-1 clobber).
        assert "SET n = props" not in query, (
            "node write must NOT use the full-replace `SET n = props` (M-1 clobber: erases "
            f"prior anchors/prov_* on re-emit); got: {query!r}"
        )


@given(entity=wm.ftm_entity())
@_SETTINGS
def test_unstamped_non_edge_entity_fails_closed(entity: FtmEntity) -> None:
    """An UNSTAMPED non-edge entity is the fail-closed node case: empty prov ⇒ must raise.

    ``wm.ftm_entity`` yields a Company-family (non-edge) entity with NO provenance stamp.
    The writer's node-provenance decision is the predicate the edge path uses —
    ``bool(provenance_node_properties(entity))`` — and it must be empty here, the signal
    that Pass 1 fails closed with ``NodeProvenanceError`` rather than write a node with no
    ``prov_*`` (ADR 0060 defect 2, the node analogue of ADR 0055).

    RED today: ``NodeProvenanceError`` does not exist (ImportError) — the type the writer
    must raise is absent, so the fail-closed contract cannot hold.
    """
    # Imported inside the body so test COLLECTION does not break before the builder adds it.
    from worldmonitor.graph.writer import EdgeProvenanceError, NodeProvenanceError

    # Mirrors EdgeProvenanceError (ADR 0055): a ValueError so callers can catch the family.
    assert issubclass(NodeProvenanceError, ValueError)
    assert NodeProvenanceError is not EdgeProvenanceError

    # The writer's "this node has no provenance" signal — empty ⇒ Pass 1 must fail closed.
    assert provenance_node_properties(entity) == {}, (
        "an unstamped entity must project NO prov_* — the empty signal the writer keys its "
        "fail-closed node check on (ADR 0060)"
    )


@given(entity=wm.source_tagged_entity())
@_SETTINGS
def test_stamped_non_edge_entity_carries_node_provenance(entity: FtmEntity) -> None:
    """A STAMPED non-edge entity projects ``prov_*`` (must NOT trip the fail-closed check).

    The other side of the contract: a properly-stamped entity yields a non-empty
    provenance projection including ``prov_source_id``, so the writer writes its node
    WITHOUT raising. Guards against the builder over-fail-closing (refusing stamped nodes).
    """
    props = provenance_node_properties(entity)
    assert props, "a stamped entity must project a non-empty prov_* node-property map"
    assert f"{PROVENANCE_NODE_PREFIX}source_id" in props, (
        "a stamped entity's node provenance must include prov_source_id (G1)"
    )
    assert props[f"{PROVENANCE_NODE_PREFIX}source_id"], "prov_source_id must be non-empty"
