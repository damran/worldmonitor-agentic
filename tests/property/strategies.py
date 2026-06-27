"""Shared hypothesis strategies + oracles for the WorldMonitor property/metamorphic harness.

NO tests live here — only the generators and comparators the ``test_prop_*`` modules build on.
Everything targets THIS repo's real interfaces (``worldmonitor.ontology.ftm.make_entity``,
``resolution.merge.cluster_and_merge``, ``resolution.canonical._anchor_id`` / ``pick_anchor``,
``guard.sensitivity.is_sensitive``, ``provenance.model.*``).

Traps these helpers encode (respect them or get FALSE failures, which would HIDE real ones):
* compare merged entities by PROPERTIES + member-set + canonical id, never raw ``to_dict()`` — FtM
  injects ``datasets`` / ``referents`` that are order-sensitive (:func:`cluster_signature`).
* a cluster's SCORE is a function of the APPLIED pair-set; permute the same pairs, never change the
  spanning structure, for an order-independence property.
* ``needs_review`` returns an order-dependent reason string — assert booleans / member-sets only.
* compare a member's contributed values via its OWN ``entity.get(prop)`` (already FtM-cleaned), so
  the lossless-union property compares clean-against-clean (FtM cleans on ``make_entity``).
"""

from __future__ import annotations

from collections.abc import Sequence

from followthemoney.types import registry
from hypothesis import strategies as st

from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.splink_model import ScoredPair

# Small id pool so member-set / source assertions shrink to minimal counterexamples and so that
# permuted inputs actually re-collide on the same ids.
ID_POOL = tuple("abcdefghij")
SOURCE_POOL = ("src-A", "src-B", "src-C", "src-D", "src-E")
# Pairwise-mergeable schemata (one descends from / equals the other within the LegalEntity family).
MERGEABLE_SCHEMATA = ("Company", "Organization", "LegalEntity")

# FtM topic vocabularies, loaded programmatically (mirror sensitivity.py's source of truth).
RISK_TOPICS = tuple(sorted(registry.topic.RISKS))
_KNOWN_NAMES = set(registry.topic.names)


def _has_risk_ancestor(code: str) -> bool:
    """True iff a dot-ANCESTOR of ``code`` (or ``code`` itself) is a RISKS code."""
    return any(code == r or code.startswith(r + ".") for r in registry.topic.RISKS)


# Clause-(b) witnesses: REAL FtM topic codes that are IN the registry vocabulary, are NOT themselves
# a RISKS code, but DO have a RISKS dot-ancestor (e.g. ``role.pep.natl`` under ``role.pep``,
# ``crime.traffick.human`` under ``crime.traffick``). These are exactly the PEP/sub-codes the
# guard's dot-ancestor walk (clause b) must flag — and the ONLY inputs that exercise it: a
# synthesised ``risk + random-suffix`` lands in the unknown-code hinge (clause c) instead. If this
# tuple is ever empty (a vocabulary change), the property below errors loudly, never skips silently.
RISK_NAMED_SUBCODES = tuple(
    sorted(c for c in _KNOWN_NAMES if c not in registry.topic.RISKS and _has_risk_ancestor(c))
)

# Known topic codes that are NOT sensitive under deny-by-default (in names, not a risk, no risk
# ancestor) — the only inputs for which ``is_sensitive`` may legitimately be False.
BENIGN_TOPICS = tuple(
    sorted(c for c in _KNOWN_NAMES if c not in registry.topic.RISKS and not _has_risk_ancestor(c))
)

_NAME = st.text(
    alphabet=st.characters(min_codepoint=65, max_codepoint=122, categories=("Lu", "Ll")),
    min_size=1,
    max_size=8,
)


def _provenance(source_id: str, entity_id: str) -> Provenance:
    return Provenance(
        source_id=source_id,
        retrieved_at="2026-01-01T00:00:00Z",
        reliability="B",
        source_record=f"s3://landing/{entity_id}.json",
    )


@st.composite
def ftm_entity(
    draw: st.DrawFn,
    *,
    entity_id: str | None = None,
    schema: str | None = None,
    extra_props: dict[str, list[str]] | None = None,
) -> FtmEntity:
    """An FtM entity (Company-family by default). ids draw from a small pool so member sets meet."""
    eid = entity_id if entity_id is not None else draw(st.sampled_from(ID_POOL))
    sch = schema if schema is not None else draw(st.sampled_from(MERGEABLE_SCHEMATA))
    props: dict[str, list[str]] = dict(extra_props or {})
    if "name" not in props:
        props["name"] = [draw(_NAME)]
    return make_entity({"id": eid, "schema": sch, "properties": props})


@st.composite
def source_tagged_entity(
    draw: st.DrawFn,
    *,
    entity_id: str | None = None,
    source_id: str | None = None,
    schema: str | None = None,
    extra_props: dict[str, list[str]] | None = None,
) -> FtmEntity:
    """An :func:`ftm_entity` stamped with a provenance ``source_id`` (values attribute to it)."""
    entity = draw(ftm_entity(entity_id=entity_id, schema=schema, extra_props=extra_props))
    src = source_id if source_id is not None else draw(st.sampled_from(SOURCE_POOL))
    return stamp(entity, _provenance(src, entity.id or ""))


# --- anchor / canonical-id strategies ---------------------------------------------------------

ANCHOR_KINDS = ("qid", "lei", "regno", "taxno")


def anchor_value() -> st.SearchStrategy[str]:
    """Adversarial NON-EMPTY raw anchor values for the ``_anchor_id`` injectivity property.

    Deliberately spans the classes most likely to alias two DISTINCT real values onto one durable
    id (a silent cross-entity merge the catastrophic-merge guard never sees):

    * clean alnum tokens (the verbatim branch);
    * sanitisation twins — values that differ only in a char ``_anchor_id`` rewrites to ``-``
      (``HRB/12`` vs ``HRB-12``, ``A:B`` vs ``A-B``, ``A B`` vs ``A-B``);
    * trailing ``.`` / ``-`` (the CID-5 non-FtM-fixed-point class);
    * values that ALREADY end in ``-<12 hex>`` (must be forced into the hashed namespace so they
      can never alias a hostile value's hashed id);
    * unicode (every non-``[A-Za-z0-9.-]`` codepoint collapses to ``-``).
    """
    clean = st.text(alphabet="ABCDEFGHIJ0123456789", min_size=1, max_size=10)
    sanitising = st.text(alphabet="ABC/_:. -123", min_size=1, max_size=8)
    trailing = st.builds(
        lambda body, tail: body + tail,
        st.text(alphabet="ABCDEF0123456789", min_size=1, max_size=6),
        st.sampled_from([".", "-", "..", "--", ".-", "-."]),
    )
    hex_tail = st.builds(
        lambda body, h: f"{body}-{h}",
        st.text(alphabet="ABCxyz0123456789", min_size=1, max_size=5),
        st.text(alphabet="0123456789abcdef", min_size=12, max_size=12),
    )
    unicode_v = st.text(
        alphabet=st.characters(min_codepoint=0x00C0, max_codepoint=0x024F), min_size=1, max_size=6
    )
    return st.one_of(clean, sanitising, trailing, hex_tail, unicode_v)


@st.composite
def anchored_entity(
    draw: st.DrawFn,
    *,
    entity_id: str,
    field: str,
    value: str,
    schema: str = "Company",
) -> FtmEntity:
    """A Company-family entity carrying one canonical anchor (``wm_anchor_<field>`` context key)."""
    entity = draw(ftm_entity(entity_id=entity_id, schema=schema))
    set_anchor(entity, field, value)
    return entity


# --- cluster strategies ------------------------------------------------------------------------


@st.composite
def connected_cluster(
    draw: st.DrawFn,
    *,
    min_members: int = 2,
    max_members: int = 5,
    schema: str = "Company",
    threshold: float = 0.92,
) -> tuple[list[FtmEntity], list[ScoredPair]]:
    """One forced-connected component: N same-schema entities + pairs (>= threshold) wiring them as
    a chain / star / complete graph, so :func:`cluster_and_merge` yields exactly one merged cluster
    whose member set is ALL N ids regardless of the wiring shape."""
    n = draw(st.integers(min_value=min_members, max_value=max_members))
    ids = list(ID_POOL[:n])
    entities = [draw(ftm_entity(entity_id=eid, schema=schema)) for eid in ids]
    structure = draw(st.sampled_from(["chain", "star", "complete"]))
    if structure == "chain":
        edges = [(ids[i], ids[i + 1]) for i in range(n - 1)]
    elif structure == "star":
        edges = [(ids[0], ids[i]) for i in range(1, n)]
    else:
        edges = [(ids[i], ids[j]) for i in range(n) for j in range(i + 1, n)]
    pairs = [
        ScoredPair(a, b, draw(st.floats(min_value=threshold, max_value=1.0))) for a, b in edges
    ]
    return entities, pairs


@st.composite
def multi_component_cluster(
    draw: st.DrawFn,
    *,
    schema: str = "Company",
) -> tuple[list[FtmEntity], list[ScoredPair], list[frozenset[str]]]:
    """2..3 DISJOINT groups, each internally wired ABOVE threshold, plus inter-group pairs strictly
    BELOW threshold (ignored). Unlike :func:`connected_cluster` (one component, member set forced
    identical under any permutation) this exercises the GROUPING decision — which id lands in which
    cluster — so an order-independence property over it can actually FAIL if clustering were
    order-dependent. Returns ``(entities, pairs, expected_groups)``."""
    n_groups = draw(st.integers(min_value=2, max_value=3))
    ids_iter = iter(ID_POOL)
    groups: list[list[str]] = []
    for _ in range(n_groups):
        size = draw(st.integers(min_value=2, max_value=3))
        groups.append([next(ids_iter) for _ in range(size)])
    entities = [draw(ftm_entity(entity_id=eid, schema=schema)) for g in groups for eid in g]
    pairs: list[ScoredPair] = []
    for g in groups:
        for i in range(len(g) - 1):
            pairs.append(ScoredPair(g[i], g[i + 1], draw(st.floats(min_value=0.92, max_value=1.0))))
    for gi in range(len(groups) - 1):
        pairs.append(
            ScoredPair(
                groups[gi][0], groups[gi + 1][0], draw(st.floats(min_value=0.0, max_value=0.9))
            )
        )
    return entities, pairs, [frozenset(g) for g in groups]


# --- comparators -------------------------------------------------------------------------------


def cluster_signature(cluster: ResolvedCluster) -> tuple[object, ...]:
    """A permutation-stable comparator: canonical id + member set + score + sorted property map.

    Compares PROPERTIES (never raw ``to_dict()``, which FtM decorates with order-sensitive
    ``datasets`` / ``referents``)."""
    props = tuple(
        sorted(
            (prop, tuple(sorted(str(v) for v in values)))
            for prop, values in cluster.entity.properties.items()
        )
    )
    return (cluster.canonical_id, tuple(sorted(cluster.member_ids)), round(cluster.score, 9), props)


def signatures(clusters: Sequence[ResolvedCluster]) -> frozenset[tuple[object, ...]]:
    """The order-independent SET of cluster signatures for a resolution result."""
    return frozenset(cluster_signature(c) for c in clusters)


def member_partition(clusters: Sequence[ResolvedCluster]) -> frozenset[frozenset[str]]:
    """The partition of member ids the clustering produced (order-independent)."""
    return frozenset(frozenset(c.member_ids) for c in clusters)
