"""Unit tests for Gate 3a-ii-B — the projection divergence measure + fence + collector sentinel
(ADR 0102).

Docker-free throughout (pure in-memory dataclasses + an in-memory SQLite session for the collector
sentinel cases, mirroring ``tests/unit/test_metrics_collector.py``'s idiom without editing that
file — the HARD RULE for this gate).

Covers (spec §4 UNIT section):
  * ``measure_divergence`` example table: identical graphs -> 0; each exclusion class
    (wm_anchor_*/datasets/prov_* added to live -> 0; differing labels -> 0); a missing fold node
    -> 1; a thinner live value-set -> 0; an extra live value -> 1; an edge endpoint alias
    resolving back -> 0; an edge with no fold counterpart -> 1.
  * ``_same_neo4j_target`` table (``worldmonitor.runner.driver``): exact match, trailing slash,
    case variant, scheme variant (same host:port), default-port handling -> True; different
    port/host -> False.
  * Collector sentinel (``worldmonitor.metrics.collector.DriverMetricsCollector``): built WITHOUT
    ``projection_divergence`` -> gauge == -1 / timestamp gauge == 0; built WITH it -> gauge ==
    ``.total`` / timestamp gauge == ``.computed_at.timestamp()``.
  * ``read_graph_snapshot`` (``worldmonitor.graph.snapshot``): value-coercion + read-only (a stub
    client that is fatal on ``execute_write``).

RED at collection time: ``worldmonitor.resolution.divergence`` (and, further down the same
collection, ``worldmonitor.runner.driver._same_neo4j_target`` and
``worldmonitor.graph.snapshot.read_graph_snapshot``) do not exist yet — the module-level imports
fail with ``ImportError``. That is the correct, intended TDD failure mode (the Gate 3a-i
precedent).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.db.models import Base
from worldmonitor.graph.snapshot import read_graph_snapshot  # gate import: RED until builder lands
from worldmonitor.metrics.collector import DriverMetricsCollector
from worldmonitor.resolution.divergence import (  # gate import: RED until builder lands
    EdgeSnapshot,
    GraphSnapshot,
    NodeSnapshot,
    ProjectionDivergence,
    measure_divergence,
)
from worldmonitor.runner.driver import _same_neo4j_target  # gate import: RED until builder lands

# ---------------------------------------------------------------------------
# SQLite JSONB shim (idempotent if already registered by another test module)
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _identity(token: str) -> str:
    return token


_NOW = datetime(2026, 7, 5, 8, 30, 0, tzinfo=UTC)


# ===========================================================================
# measure_divergence — example table (ADR 0102 D6)
# ===========================================================================


def test_identical_graphs_zero_divergence() -> None:
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1", labels=frozenset({"Company"}), props={"name": frozenset({"Acme"})}
            ),
        ),
        edges=(),
    )
    live = fold  # byte-identical
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.unexplained_nodes == 0
    assert result.unexplained_edges == 0
    assert result.total == 0
    assert result.live_nodes == 1
    assert result.live_edges == 0


def test_wm_anchor_prop_added_to_live_is_excluded() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={"name": frozenset({"Acme"})}),),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1",
                labels=frozenset(),
                props={"name": frozenset({"Acme"}), "wm_anchor_qid": frozenset({"Q123"})},
            ),
        ),
        edges=(),
    )
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.total == 0, "wm_anchor_* (E2) must be excluded from the compared-prop set"


def test_datasets_prop_added_to_live_is_excluded() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={"name": frozenset({"Acme"})}),),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1",
                labels=frozenset(),
                props={"name": frozenset({"Acme"}), "datasets": frozenset({"live-batch"})},
            ),
        ),
        edges=(),
    )
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.total == 0, "'datasets' (E4) must be excluded from the compared-prop set"


def test_prov_props_added_to_live_are_excluded() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={"name": frozenset({"Acme"})}),),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1",
                labels=frozenset(),
                props={
                    "name": frozenset({"Acme"}),
                    "prov_source_id": frozenset({"src:live"}),
                    "prov_witnesses": frozenset({"{}"}),
                },
            ),
        ),
        edges=(),
    )
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.total == 0, "prov_* scalars AND prov_witnesses must be excluded (D6-ii)"


def test_caption_pick_shift_is_excluded() -> None:
    """D6-iii (the HIGH adversarial-verify finding): ``caption`` is a single FtM *pick*, not a
    union-monotone value set. A routine cross-batch name update leaves the live node's caption
    at its LAST write's pick ('Robert Smith') while the fold picks over the WHOLE-log name
    union ('Bob Smith') — a legitimate divergence that must NOT count, or the alert would fire
    permanently on real data. The caption's inputs (the name values) stay fully compared."""
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="p1",
                labels=frozenset(),
                props={
                    "name": frozenset({"Bob Smith", "Robert Smith"}),
                    "caption": frozenset({"Bob Smith"}),  # fold's pick over the whole-log union
                },
            ),
        ),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="p1",
                labels=frozenset(),
                props={
                    "name": frozenset({"Robert Smith"}),  # thinner re-emit — subset, fine
                    "caption": frozenset({"Robert Smith"}),  # last write's pick — differs
                },
            ),
        ),
        edges=(),
    )
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.total == 0, (
        "'caption' (a picked scalar, not a union-monotone set) must be excluded (D6-iii) — a "
        "cross-batch name update would otherwise false-alarm forever"
    )

    # And the inputs stay guarded: an unexplained NAME value still counts.
    rotten = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="p1",
                labels=frozenset(),
                props={
                    "name": frozenset({"Robert Smith", "EVIL INJECTED"}),
                    "caption": frozenset({"whatever"}),
                },
            ),
        ),
        edges=(),
    )
    assert measure_divergence(rotten, fold, _identity, computed_at=_NOW).total == 1, (
        "excluding 'caption' must NOT blind the measure to unexplained NAME values"
    )


def test_differing_labels_do_not_cause_divergence() -> None:
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1", labels=frozenset({"Company"}), props={"name": frozenset({"Acme"})}
            ),
        ),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1",
                labels=frozenset({"Company", "LegalEntity", "Extra"}),
                props={"name": frozenset({"Acme"})},
            ),
        ),
        edges=(),
    )
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.total == 0, "labels are NEVER compared (D6-i)"


def test_missing_fold_node_counts_as_one_unexplained() -> None:
    fold = GraphSnapshot(nodes=(), edges=())
    live = GraphSnapshot(nodes=(NodeSnapshot(id="ghost", labels=frozenset(), props={}),), edges=())
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.unexplained_nodes == 1
    assert result.total == 1


def test_thinner_live_value_set_is_explained() -> None:
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(id="s1", labels=frozenset(), props={"alias": frozenset({"a", "b", "c"})}),
        ),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={"alias": frozenset({"a"})}),),
        edges=(),
    )
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.total == 0, "a live value-set that is a SUBSET of the fold's must be explained"


def test_extra_live_value_causes_divergence() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={"alias": frozenset({"a"})}),),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={"alias": frozenset({"a", "z"})}),),
        edges=(),
    )
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.unexplained_nodes == 1
    assert result.total == 1


def test_edge_endpoint_alias_resolving_back_is_explained() -> None:
    def survivor_of(token: str) -> str:
        return {"old-owner": "owner-survivor"}.get(token, token)

    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(id="owner-survivor", labels=frozenset(), props={}),
            NodeSnapshot(id="asset1", labels=frozenset(), props={}),
        ),
        edges=(EdgeSnapshot(type="OWNS", src="owner-survivor", dst="asset1", props={}),),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(id="old-owner", labels=frozenset(), props={}),
            NodeSnapshot(id="asset1", labels=frozenset(), props={}),
        ),
        edges=(EdgeSnapshot(type="OWNS", src="old-owner", dst="asset1", props={}),),
    )
    result = measure_divergence(live, fold, survivor_of, computed_at=_NOW)
    assert result.total == 0, (
        "an edge endpoint alias that survivor_of resolves back must be explained"
    )


def test_edge_with_no_fold_counterpart_counts_as_one_unexplained() -> None:
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(id="a", labels=frozenset(), props={}),
            NodeSnapshot(id="b", labels=frozenset(), props={}),
        ),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(id="a", labels=frozenset(), props={}),
            NodeSnapshot(id="b", labels=frozenset(), props={}),
        ),
        edges=(EdgeSnapshot(type="OWNS", src="a", dst="b", props={}),),
    )
    result = measure_divergence(live, fold, _identity, computed_at=_NOW)
    assert result.unexplained_edges == 1
    assert result.total == 1


# ===========================================================================
# _same_neo4j_target — the D3 misconfig-fence comparison table
# ===========================================================================


@pytest.mark.parametrize(
    ("live_uri", "diff_uri", "expected"),
    [
        ("bolt://neo4j:7687", "bolt://neo4j:7687", True),  # exact match
        ("bolt://neo4j:7687", "bolt://neo4j:7687/", True),  # trailing slash
        ("bolt://neo4j:7687", "BOLT://NEO4J:7687", True),  # case variant
        ("bolt://neo4j:7687", "neo4j://neo4j:7687", True),  # scheme variant, same host:port
        ("bolt://h:7687", "neo4j://h", True),  # default-port handling (absent port -> 7687)
        # --- host-ALIAS variants (the CRITICAL adversarial-verify finding): the loopback
        # equivalence class must collapse to one canonical token, textual IP variants must
        # normalize, and trailing-dot FQDNs must strip — else the fence is bypassed and the
        # guard wipes the LIVE graph (bolt://localhost:7687 is the SHIPPED default live URI).
        ("bolt://localhost:7687", "bolt://127.0.0.1:7687", True),  # localhost vs IPv4 loopback
        ("bolt://localhost:7687", "bolt://[::1]:7687", True),  # localhost vs IPv6 loopback
        ("bolt://[::1]:7687", "bolt://[0:0:0:0:0:0:0:1]:7687", True),  # IPv6 textual variants
        ("bolt://127.0.0.1:7687", "bolt://127.0.0.2:7687", True),  # whole 127.0.0.0/8 class
        ("bolt://localhost:7687", "bolt://0.0.0.0:7687", True),  # unspecified ⇒ loopback class
        ("bolt://neo4j:7687", "bolt://neo4j.:7687", True),  # trailing-dot FQDN
        ("bolt://neo4j:7687", "bolt://neo4j:7688", False),  # different port
        ("bolt://neo4j:7687", "bolt://other:7687", False),  # different host
        ("bolt://localhost:7687", "bolt://10.0.0.5:7687", False),  # loopback vs a real host
    ],
)
def test_same_neo4j_target_table(live_uri: str, diff_uri: str, expected: bool) -> None:
    assert _same_neo4j_target(live_uri, diff_uri) is expected, (
        f"_same_neo4j_target({live_uri!r}, {diff_uri!r}) expected {expected} — the D3 misconfig "
        "fence must be biased toward MORE refusals: a false refusal is merely annoying, a missed "
        "match is catastrophic (ADR 0102 D3)."
    )


# ===========================================================================
# Collector sentinel (worldmonitor.metrics.collector.DriverMetricsCollector)
# ===========================================================================


class _StubNeo4j:
    """A read-only Neo4j stand-in for the collector's own gauges — canned counts, fatal write."""

    def execute_read(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        is_edges = "[r]" in query or "->()" in query or "count(r)" in query
        return [{"n": 0 if is_edges else 0}]

    def execute_write(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        raise AssertionError("collector must be read-only — execute_write must never be called")

    def close(self) -> None:
        pass


def _sqlite_sessions() -> sessionmaker[Session]:
    """A real session factory over a single shared in-memory SQLite DB (Docker-free)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _collect(
    collector: DriverMetricsCollector,
) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    """Flatten one ``collect()`` scrape into ``{(name, labels) -> value}``."""
    values: dict[tuple[str, frozenset[tuple[str, str]]], float] = {}
    for family in collector.collect():
        for sample in family.samples:
            values[(sample.name, frozenset(sample.labels.items()))] = sample.value
    return values


def test_collector_projection_divergence_sentinel_when_not_wired() -> None:
    """Built WITHOUT ``projection_divergence`` -> gauge -1, liveness-timestamp gauge 0."""
    sessions = _sqlite_sessions()
    collector = DriverMetricsCollector(
        session_factory=sessions, neo4j=_StubNeo4j(), skip_counter=lambda: 0
    )
    values = _collect(collector)
    assert values[("worldmonitor_projection_divergence", frozenset())] == -1.0
    assert values[("worldmonitor_projection_divergence_last_run_timestamp", frozenset())] == 0.0


def test_collector_projection_divergence_reports_total_when_wired() -> None:
    """Built WITH ``projection_divergence`` -> gauge == .total, timestamp gauge == .computed_at."""
    sessions = _sqlite_sessions()
    div = ProjectionDivergence(
        unexplained_nodes=2, unexplained_edges=3, live_nodes=10, live_edges=5, computed_at=_NOW
    )
    collector = DriverMetricsCollector(
        session_factory=sessions,
        neo4j=_StubNeo4j(),
        skip_counter=lambda: 0,
        projection_divergence=lambda: div,
    )
    values = _collect(collector)
    assert values[("worldmonitor_projection_divergence", frozenset())] == 5.0
    assert values[("worldmonitor_projection_divergence_last_run_timestamp", frozenset())] == (
        _NOW.timestamp()
    )


# ===========================================================================
# read_graph_snapshot — value-coercion + read-only (worldmonitor.graph.snapshot)
# ===========================================================================


class _SnapshotStubClient:
    """A stub Neo4j client: canned node/edge rows; ``execute_write`` is fatal (read-only proof)."""

    def __init__(self) -> None:
        self.write_called = False

    def execute_read(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        if "RETURN n.id AS nid" in query:
            return [
                {
                    "nid": "s1",
                    "lbls": ["Company", "LegalEntity"],
                    "props": {"name": "Acme", "topics": ["a", "b"]},
                }
            ]
        if "RETURN type(r) AS rtype" in query:
            return [{"rtype": "OWNS", "src": "s1", "dst": "s2", "rprops": {"since": ["2020"]}}]
        raise AssertionError(f"unexpected read_graph_snapshot query: {query!r}")

    def execute_write(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        self.write_called = True
        raise AssertionError("read_graph_snapshot must NEVER write (D9 read-only contract)")


def test_read_graph_snapshot_coerces_values_and_is_read_only() -> None:
    client = _SnapshotStubClient()
    snap = read_graph_snapshot(client)

    assert isinstance(snap, GraphSnapshot)
    node = next(n for n in snap.nodes if n.id == "s1")
    assert node.labels == frozenset({"Company", "LegalEntity"})
    assert node.props["name"] == frozenset({"Acme"}), "a scalar Neo4j value coerces to {str(v)}"
    assert node.props["topics"] == frozenset({"a", "b"}), "a list Neo4j value coerces per-element"

    assert len(snap.edges) == 1
    edge = snap.edges[0]
    assert edge.type == "OWNS"
    assert edge.src == "s1"
    assert edge.dst == "s2"
    assert edge.props["since"] == frozenset({"2020"})

    assert client.write_called is False, "read_graph_snapshot must never call execute_write"
