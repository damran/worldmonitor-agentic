"""Gate B-4b — cross-store backup / restore disaster recovery (ADR 0050).

B persists its entire system of record across THREE stores and, before this module, could recover
NONE of it:

* **Postgres** — the human decisions (the H-1 ``resolver_judgement`` negatives/positives,
  ``sign_off``, ``merge_audit``, the no-un-merge ``canonical_id_ledger``) plus ``er_queue_item`` /
  ``ingest_dead_letter`` / ``task_run`` / ``merge_alerts`` / ``er_gold_pair`` /
  ``connector_instance``, and the ``alembic_version`` row.
* **Neo4j** — the resolved graph (the product), with ``prov_*`` G1 provenance on every node + edge.
* **MinIO** — the raw bytes behind every ``source_record`` provenance pointer.

:func:`backup` writes a point-in-time logical dump of all three stores under ``dest`` and
:func:`restore` rebuilds them by wipe + reload. The correctness bar is NOT "the process exits 0":
a restore that silently dropped the ``resolver_judgement`` NEGATIVES would re-enable the exact H-1
transitive re-merge ADR 0037 closes — now reached via disaster recovery — so the load-bearing
guarantee is **the human-reject decision and the canonical-id ledger survive DR byte-for-byte**.

Design (ADR 0050 / spec ``GATE_B4B_BACKUP_RESTORE_SPEC.md``):

* **Postgres** is a SQLAlchemy *logical* dump of every ``Base.metadata`` table (and ``alembic``):
  lossless for B's schema (all PKs are ``String`` — no sequences; one app DB; JSONB round-trips as
  JSON) and assertable in-process. Restore TRUNCATEs + bulk-inserts + re-stamps ``alembic_version``
  so a post-restore ``migrate_to_head`` is a no-op.
* **Neo4j** is an *online* Cypher logical export (no neo4j stop / no outage / version-portable):
  nodes (labels + properties incl. ``id`` / ``name`` / every ``prov_*`` / ``prov_witnesses``) and
  edges (type + endpoint ``id``s + edge ``prov_*``). The graph keys on the ``id`` property
  (ADR 0042), so the APPLICATION canonical id round-trips byte-for-byte; Neo4j internal element ids
  (which nothing references) need not. Restore DETACH-DELETEs all, re-establishes the idempotent
  ``ensure_constraints``, then MERGEs nodes (grouped by label-set) and re-creates edges (grouped by
  type). Labels/types are APOC-free, inlined as ``LiteralString`` only after passing BOTH a strict
  identifier regex AND membership in the closed, validated FtM-schema/topic vocabulary (the same
  closed-vocabulary-inlining idiom ``guard/sensitivity.py`` uses for its k-hop bound).
* **MinIO** is an object mirror (paged past the 1000-key cap via ``LandingStore.list_keys``) plus an
  explicit ``landing_index.json`` — an empty bucket is an EXPLICIT empty list, never a missing
  artifact.

HALT-LOUD (defending against Workflow A's ``a5bb7d5`` robustness bug class by construction):

* backup writes ``manifest.json`` with ``complete:true`` LAST, only after every store artifact is
  present + count-verified, and RAISES on any store-export failure (no silent partial backup);
* restore VALIDATES the manifest + all three artifacts BEFORE touching any store (no half-wipe),
  stages Postgres in a SINGLE transaction the caller commits only after Neo4j + MinIO succeed, and
  re-verifies every store's count against the manifest — a half-restore can never RETURN success;
* zero objects / zero nodes / zero edges are EXPLICIT empty collections, never missing artifacts.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Table, func, inspect, select, text
from sqlalchemy.orm import Session

from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.db.models import Base
from worldmonitor.graph.constraints import ENTITY_LABEL, ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)

# --- on-disk layout under dest/ ------------------------------------------------------------------
_MANIFEST = "manifest.json"
_POSTGRES = "postgres.json"
_NEO4J = "neo4j.json"
_LANDING_INDEX = "landing_index.json"
_LANDING_DIR = "landing"

# Alembic's own version table is NOT a Base.metadata table; it is captured + re-stamped explicitly.
_ALEMBIC_TABLE = "alembic_version"

# A label/relationship-type token may only be inlined into a Cypher ``LiteralString`` if it is a
# bare identifier (no backtick / space / colon / Cypher metacharacter): this regex is the security
# guarantee that makes the inlining injection-proof, applied ON TOP OF the closed-vocabulary check.
_SAFE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """The completion marker + per-store counts written to ``manifest.json`` (spec §4.4)."""

    created_at: str
    alembic_version: str | None
    postgres_row_counts: dict[str, int]
    neo4j_nodes: int
    neo4j_edges: int
    landing_objects: int
    complete: bool

    def as_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk manifest shape (``neo4j`` / ``landing`` nested counts)."""
        return {
            "created_at": self.created_at,
            "alembic_version": self.alembic_version,
            "postgres_row_counts": self.postgres_row_counts,
            "neo4j": {"nodes": self.neo4j_nodes, "edges": self.neo4j_edges},
            "landing": {"objects": self.landing_objects},
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Post-restore re-verified per-store counts (== the manifest, or :func:`restore` raised)."""

    postgres_row_counts: dict[str, int]
    neo4j_nodes: int
    neo4j_edges: int
    landing_objects: int


# ============================================= closed label/type vocabulary (APOC-free) ==========


@lru_cache(maxsize=1)
def _allowed_node_labels() -> frozenset[str]:
    """The closed set of node labels restore may inline (FtM schema + topic labels + base labels).

    Built from the SAME ftmg ``Configuration`` the writer (``graph/writer.py``) and the sensitivity
    guard (``guard/sensitivity.py:_risk_labels``) build, so it tracks the FtM/ftmg pin automatically
    and never hardcodes the PascalCase casing. ``Configuration``'s db creds drive only the
    label/transform logic — no connection is opened here. Imported lazily so a non-graph use of this
    module never pulls in the ftmg dependency.
    """
    from followthemoney.types import registry
    from ftmg.config import Configuration, DatabaseConfig  # type: ignore[import-untyped]

    from worldmonitor.settings import get_settings

    settings = get_settings()
    pw = settings.neo4j_password.get_secret_value()
    config: Any = Configuration(
        path=Path("."),
        db=DatabaseConfig(url=settings.neo4j_uri, username=settings.neo4j_user, password=pw),
    )
    labels: set[str] = {ENTITY_LABEL, "Ghost"}
    labels.update(str(schema.label) for schema in config.nodes.schemata.values())
    labels.update(str(config.nodes.topics[code].label) for code in registry.topic.names)
    return frozenset(labels)


@lru_cache(maxsize=1)
def _allowed_edge_types() -> frozenset[str]:
    """The closed set of relationship types restore inlines (FtM edge-schema + entity-link types).

    Edge SCHEMA labels (``Ownership → OWNS``) and entity-link property labels
    (``LegalEntity:subsidiaries → SUBSIDIARIES``) are the only relationship types the writer emits,
    read off the same ftmg ``Configuration`` (no connection opened) so the set tracks the FtM pin.
    """
    from ftmg.config import Configuration, DatabaseConfig  # type: ignore[import-untyped]

    from worldmonitor.settings import get_settings

    settings = get_settings()
    pw = settings.neo4j_password.get_secret_value()
    config: Any = Configuration(
        path=Path("."),
        db=DatabaseConfig(url=settings.neo4j_uri, username=settings.neo4j_user, password=pw),
    )
    types: set[str] = set()
    types.update(str(schema.label) for schema in config.edges.schemata.values())
    types.update(str(prop.label) for prop in config.edges.properties.values())
    return frozenset(types)


def _validate_token(token: str, allowed: frozenset[str], kind: str) -> str:
    """Return ``token`` iff it is a bare identifier AND in the closed vocabulary; else raise.

    Fail-closed: a token that is not a safe identifier (could break the ``LiteralString``) OR is
    outside the validated FtM/topic vocabulary halts the restore rather than being inlined.
    """
    if not _SAFE_TOKEN.match(token) or token not in allowed:
        raise ValueError(
            f"backup/restore: refusing to inline an unrecognized {kind} {token!r} "
            "(not a bare identifier in the closed FtM-schema/topic vocabulary)"
        )
    return token


# ===================================================================================== JSON helpers


def _json_default(value: Any) -> str:
    """Serialize a Postgres ``datetime`` as an ISO-8601 string (restore re-types it per column)."""
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"backup: value of type {type(value).__name__} is not JSON-serializable")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, default=_json_default, sort_keys=True, indent=2))


# ===================================================== Postgres store ============================


def _export_postgres(session: Session) -> dict[str, Any]:
    """Logical dump of every ``Base.metadata`` table + the ``alembic_version`` row (read-only)."""
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in Base.metadata.sorted_tables:
        rows = session.execute(select(table)).mappings()
        tables[table.name] = [{col.name: row[col.name] for col in table.columns} for row in rows]
    bind = session.get_bind()
    alembic_version: str | None = None
    if _ALEMBIC_TABLE in inspect(bind).get_table_names():
        alembic_version = session.execute(
            text(f"SELECT version_num FROM {_ALEMBIC_TABLE}")  # noqa: S608 - fixed table constant
        ).scalar_one_or_none()
    return {"tables": tables, "alembic_version": alembic_version}


def _typed_row(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    """Re-type a JSON row for bulk insert (ISO strings → ``datetime`` for ``DateTime`` columns)."""
    typed: dict[str, Any] = {}
    for column in table.columns:
        if column.name not in row:
            continue
        value = row[column.name]
        if isinstance(value, str) and isinstance(column.type, DateTime):
            value = datetime.fromisoformat(value)
        typed[column.name] = value
    return typed


def _import_postgres(session: Session, data: dict[str, Any]) -> None:
    """TRUNCATE every table + bulk-insert every row + re-stamp ``alembic_version`` (single txn).

    Staged on ``session`` — the caller commits only after Neo4j + MinIO also succeed, so a later
    failure rolls the entire relational restore back (all-or-nothing; A's ``a5bb7d5`` #4).
    """
    tables = list(Base.metadata.sorted_tables)
    bind = session.get_bind()
    has_alembic = _ALEMBIC_TABLE in inspect(bind).get_table_names()
    # Table names are in-code Base.metadata constants (never external input) -> S608 not applicable.
    truncate_sql = (
        "TRUNCATE " + ", ".join(t.name for t in tables) + " RESTART IDENTITY CASCADE"  # noqa: S608
    )
    session.execute(text(truncate_sql))
    table_dump: dict[str, Any] = data.get("tables", {})
    for table in tables:
        rows = table_dump.get(table.name, [])
        if rows:
            session.execute(table.insert(), [_typed_row(table, row) for row in rows])
    if has_alembic:
        session.execute(text(f"DELETE FROM {_ALEMBIC_TABLE}"))  # noqa: S608 - fixed table constant
        revision = data.get("alembic_version")
        if revision is not None:
            session.execute(
                text(f"INSERT INTO {_ALEMBIC_TABLE} (version_num) VALUES (:rev)"),  # noqa: S608
                {"rev": revision},
            )


def _postgres_counts(session: Session) -> dict[str, int]:
    """Per-table row counts (reads the caller's transaction, so it sees staged restore writes)."""
    return {
        table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
        for table in Base.metadata.sorted_tables
    }


# ======================================================= Neo4j store =============================


def _export_neo4j(neo4j: Neo4jClient) -> dict[str, list[dict[str, Any]]]:
    """Online Cypher logical export: nodes (labels + props) + edges (type + endpoint ids + props).

    Zero nodes / zero edges serialize as EXPLICIT empty lists, never a missing key (A's ``a5bb7d5``
    #3). Every ``prov_*`` / ``prov_witnesses`` property is captured (G1).
    """
    node_rows = neo4j.execute_read("MATCH (n) RETURN labels(n) AS labels, properties(n) AS props")
    edge_rows = neo4j.execute_read(
        "MATCH (a)-[r]->(b) "
        "RETURN type(r) AS type, a.id AS start, b.id AS end, properties(r) AS props"
    )
    nodes = [{"labels": row["labels"], "props": row["props"]} for row in node_rows]
    edges = [
        {"type": row["type"], "start": row["start"], "end": row["end"], "props": row["props"]}
        for row in edge_rows
    ]
    return {"nodes": nodes, "edges": edges}


def _import_neo4j(neo4j: Neo4jClient, data: dict[str, Any]) -> None:
    """Wipe the graph, re-establish constraints, MERGE nodes (per label-set), re-create edges.

    Node label-sets and relationship types are inlined as validated ``LiteralString``s (closed
    vocabulary + safe-identifier regex). Edges are re-created with ``CREATE`` on the freshly
    DETACH-DELETEd graph so EVERY edge — including parallel edges of the same type between the same
    endpoints — round-trips with its exact multiplicity (a MERGE keyed only on type+endpoints would
    silently collapse parallels, breaking the byte-identical edge-count invariant); restore is
    idempotent at the operation level because it always wipes first.
    """
    neo4j.execute_write("MATCH (n) DETACH DELETE n")
    ensure_constraints(neo4j)

    allowed_labels = _allowed_node_labels()
    by_label_set: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for node in data.get("nodes", []):
        label_key = tuple(sorted(node.get("labels", [])))
        by_label_set[label_key].append(node["props"])
    for label_key, props_list in by_label_set.items():
        label_clause = "".join(
            f":{_validate_token(label, allowed_labels, 'node label')}" for label in label_key
        )
        query = "UNWIND $rows AS row MERGE (n {id: row.id}) SET n = row.props"
        if label_clause:
            query += f" SET n{label_clause}"
        rows = [{"id": props.get("id"), "props": props} for props in props_list]
        neo4j.execute_write(query, rows=rows)

    allowed_types = _allowed_edge_types()
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in data.get("edges", []):
        by_type[edge["type"]].append(edge)
    for edge_type, edges in by_type.items():
        safe_type = _validate_token(edge_type, allowed_types, "relationship type")
        query = (
            "UNWIND $rows AS row "
            "MATCH (a {id: row.start}) MATCH (b {id: row.end}) "
            f"CREATE (a)-[r:{safe_type}]->(b) SET r = row.props"
        )
        rows = [{"start": e["start"], "end": e["end"], "props": e["props"]} for e in edges]
        neo4j.execute_write(query, rows=rows)


def _neo4j_counts(neo4j: Neo4jClient) -> tuple[int, int]:
    nodes = neo4j.execute_read("MATCH (n) RETURN count(n) AS c")[0]["c"]
    edges = neo4j.execute_read("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]
    return int(nodes), int(edges)


# ====================================================== MinIO landing ============================


def _export_landing(landing: LandingStore, dest: Path) -> list[str]:
    """Mirror every landing object's bytes to ``dest/landing/<key>``; return the explicit key list.

    ``list_keys("")`` pages past the 1000-key cap. An empty bucket returns ``[]`` (an EXPLICIT empty
    collection, never a missing artifact — A's ``a5bb7d5`` #3).
    """
    landing_dir = dest / _LANDING_DIR
    landing_dir.mkdir(parents=True, exist_ok=True)
    base = landing_dir.resolve()
    keys = landing.list_keys("")
    for key in keys:
        target = (landing_dir / key).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"backup: landing key {key!r} escapes the backup directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(landing.get(key))
    return keys


def _import_landing(landing: LandingStore, src: Path, keys: list[str]) -> None:
    """Re-create the bucket, clear it, and re-``put`` every backed-up object's bytes."""
    landing.ensure_bucket()
    landing.delete_prefix("")
    landing_dir = src / _LANDING_DIR
    base = landing_dir.resolve()
    for key in keys:
        source = (landing_dir / key).resolve()
        if not source.is_relative_to(base):
            raise ValueError(f"restore: landing key {key!r} escapes the backup directory")
        landing.put(key, source.read_bytes())


# ============================================================ backup =============================


def _verify_backup_artifacts(dest: Path, manifest: BackupManifest) -> None:
    """Re-read every artifact + check its count against the manifest BEFORE marking complete.

    So ``complete:true`` is written only when all three stores are provably captured — an operator
    can never ``down -v`` believing a broken backup succeeded (A's ``a5bb7d5`` #2).
    """
    postgres = json.loads((dest / _POSTGRES).read_text())
    counts = {name: len(rows) for name, rows in postgres.get("tables", {}).items()}
    if counts != manifest.postgres_row_counts:
        raise RuntimeError("backup verification failed: postgres.json row counts mismatch")

    neo = json.loads((dest / _NEO4J).read_text())
    if len(neo.get("nodes", [])) != manifest.neo4j_nodes:
        raise RuntimeError("backup verification failed: neo4j.json node count mismatch")
    if len(neo.get("edges", [])) != manifest.neo4j_edges:
        raise RuntimeError("backup verification failed: neo4j.json edge count mismatch")

    index = json.loads((dest / _LANDING_INDEX).read_text())
    if len(index) != manifest.landing_objects:
        raise RuntimeError("backup verification failed: landing_index.json object count mismatch")
    for key in index:
        if not (dest / _LANDING_DIR / key).exists():
            raise RuntimeError(f"backup verification failed: missing landing object {key!r}")


def backup(
    *, neo4j: Neo4jClient, session: Session, landing: LandingStore, dest: Path
) -> BackupManifest:
    """Dump all three stores to ``dest`` and return a verified, complete :class:`BackupManifest`.

    Read-only on the live system (safe to schedule). HALT-LOUD: any store-export failure propagates
    and NO ``complete:true`` manifest is written; the manifest is written LAST, only after every
    artifact is present + count-verified.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    postgres = _export_postgres(session)
    _write_json(dest / _POSTGRES, postgres)

    neo = _export_neo4j(neo4j)
    _write_json(dest / _NEO4J, neo)

    keys = _export_landing(landing, dest)
    _write_json(dest / _LANDING_INDEX, keys)

    manifest = BackupManifest(
        created_at=datetime.now(UTC).isoformat(),
        alembic_version=postgres.get("alembic_version"),
        postgres_row_counts={name: len(rows) for name, rows in postgres["tables"].items()},
        neo4j_nodes=len(neo["nodes"]),
        neo4j_edges=len(neo["edges"]),
        landing_objects=len(keys),
        complete=True,
    )
    _verify_backup_artifacts(dest, manifest)
    _write_json(dest / _MANIFEST, manifest.as_dict())
    logger.info(
        "backup complete: %s postgres rows, %s nodes, %s edges, %s landing objects",
        sum(manifest.postgres_row_counts.values()),
        manifest.neo4j_nodes,
        manifest.neo4j_edges,
        manifest.landing_objects,
    )
    return manifest


# =========================================================== restore =============================


def _validate_restore_source(src: Path) -> dict[str, Any]:
    """Validate the manifest + ALL three artifacts — incl. the landing byte-files and the Neo4j
    payload's node ids / labels / edge types — BEFORE any store is touched, else raise.

    A missing / ``complete:false`` / missing-or-corrupt-artifact backup aborts with NOTHING touched
    (validate-before-wipe — A's ``a5bb7d5`` #4): the canonical DR input is a copied / rsync'd /
    bit-rotted backup, and the Neo4j + MinIO imports are non-transactional (auto-committed, not
    rolled back), so this pre-flight is the load-bearing half of the guarantee — anything an
    ``_import_*`` step would raise on (a missing landing object, a null node id, an off-vocabulary
    label / edge type) must be caught HERE, before the destructive ``DETACH DELETE`` / bucket wipe.
    """
    manifest_path = src / _MANIFEST
    if not manifest_path.exists():
        raise FileNotFoundError(f"restore: no manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("complete") is not True:
        raise ValueError(
            f"restore: refusing to restore an incomplete backup at {src} (manifest complete!=true)"
        )
    for artifact in (_POSTGRES, _NEO4J, _LANDING_INDEX):
        path = src / artifact
        if not path.exists():
            raise FileNotFoundError(f"restore: complete manifest but missing artifact {path}")
        json.loads(path.read_text())  # must be parseable

    # Pre-validate the Neo4j payload so a null id / off-vocabulary label / bad edge type is caught
    # BEFORE _import_neo4j's destructive DETACH DELETE (validate-before-touch).
    neo = json.loads((src / _NEO4J).read_text())
    allowed_labels = _allowed_node_labels()
    for node in neo.get("nodes", []):
        if not node.get("props", {}).get("id"):
            raise ValueError("restore: neo4j artifact has a node with a missing/empty id")
        for label in node.get("labels", []):
            _validate_token(label, allowed_labels, "node label")
    allowed_types = _allowed_edge_types()
    for edge in neo.get("edges", []):
        _validate_token(edge["type"], allowed_types, "relationship type")
        if not edge.get("start") or not edge.get("end"):
            raise ValueError("restore: neo4j artifact has an edge with a missing endpoint id")

    # Pre-validate every landing object byte-file the index references exists + is path-safe, BEFORE
    # _import_landing wipes the live bucket (mirror _verify_backup_artifacts at restore time). A
    # backup corrupted AFTER creation (valid index + complete manifest, but a missing object file)
    # must NOT pass validation and then destroy the live stores.
    keys = json.loads((src / _LANDING_INDEX).read_text())
    landing_dir = src / _LANDING_DIR
    base = landing_dir.resolve()
    for key in keys:
        obj = (landing_dir / key).resolve()
        if not obj.is_relative_to(base):
            raise ValueError(f"restore: landing key {key!r} escapes the backup directory")
        if not obj.exists():
            raise FileNotFoundError(
                f"restore: landing_index lists {key!r} but {obj} is missing (corrupt backup)"
            )
    return manifest


def _verify_restore(
    *,
    neo4j: Neo4jClient,
    session: Session,
    landing: LandingStore,
    manifest: dict[str, Any],
) -> RestoreResult:
    """Re-count all three stores and raise if any count != the manifest (no silent half-restore)."""
    postgres_counts = _postgres_counts(session)
    if postgres_counts != manifest["postgres_row_counts"]:
        raise RuntimeError(
            "restore verification failed: postgres row counts do not match the manifest"
        )
    nodes, edges = _neo4j_counts(neo4j)
    if nodes != manifest["neo4j"]["nodes"] or edges != manifest["neo4j"]["edges"]:
        raise RuntimeError("restore verification failed: neo4j counts do not match the manifest")
    landing_objects = len(landing.list_keys(""))
    if landing_objects != manifest["landing"]["objects"]:
        raise RuntimeError("restore verification failed: landing object count != the manifest")
    return RestoreResult(
        postgres_row_counts=postgres_counts,
        neo4j_nodes=nodes,
        neo4j_edges=edges,
        landing_objects=landing_objects,
    )


def restore(
    *, neo4j: Neo4jClient, session: Session, landing: LandingStore, src: Path
) -> RestoreResult:
    """Rebuild all three stores from a verified backup at ``src`` (destructive — a runbook action).

    Validate-before-touch + halt-loud + idempotent-retry: fully validates the manifest + every
    artifact (incl. landing byte-files + the Neo4j node ids/labels/edge types) BEFORE touching any
    store, so a corrupt/partial backup raises with NOTHING wiped. The Postgres rebuild is staged on
    ``session`` (the CALLER commits only after Neo4j + MinIO succeed, so a later failure rolls the
    relational restore back); Neo4j + MinIO are NOT transactional — but restore always wipes-then-
    rebuilds, so re-running the SAME (immutable) backup is idempotent. Re-verifies every store's
    count against the manifest. Reproduces — never re-clusters / un-merges / re-scores — the
    human-decision rows + the ``canonical_id_ledger`` byte-for-byte (H-1 + canonical-id identity).
    """
    src = Path(src)
    manifest = _validate_restore_source(src)
    postgres = json.loads((src / _POSTGRES).read_text())
    neo = json.loads((src / _NEO4J).read_text())
    keys = json.loads((src / _LANDING_INDEX).read_text())

    # Postgres first (staged, uncommitted) so a Neo4j/MinIO failure rolls the relational data back.
    _import_postgres(session, postgres)
    _import_neo4j(neo4j, neo)
    _import_landing(landing, src, keys)

    result = _verify_restore(neo4j=neo4j, session=session, landing=landing, manifest=manifest)
    logger.info(
        "restore staged: %s postgres rows, %s nodes, %s edges, %s landing objects "
        "(commit pending on the caller's session)",
        sum(result.postgres_row_counts.values()),
        result.neo4j_nodes,
        result.neo4j_edges,
        result.landing_objects,
    )
    return result


# =============================================================== CLI =============================


def main(argv: list[str] | None = None) -> None:
    """``python -m worldmonitor.backup {backup,restore} <dir>`` — build the stores from settings.

    The thin ``deploy/backup/{backup,restore}.sh`` wrappers ``exec`` this; all logic lives here.
    Logs go to stderr (never stdout) so the CLI is safe to compose with other tooling.
    """
    parser = argparse.ArgumentParser(
        prog="python -m worldmonitor.backup",
        description="Cross-store backup / restore disaster recovery (Gate B-4b, ADR 0050).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="dump all three stores to a directory")
    backup_parser.add_argument("directory", help="destination directory for the backup")
    restore_parser = subparsers.add_parser(
        "restore", help="rebuild all three stores from a backup directory (DESTRUCTIVE)"
    )
    restore_parser.add_argument("directory", help="source backup directory to restore from")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    neo4j = Neo4jClient.from_settings()
    landing = LandingStore.from_settings()
    engine = engine_from_settings()
    sessions = session_factory(engine)
    try:
        if args.command == "backup":
            with sessions() as session:
                backup(neo4j=neo4j, session=session, landing=landing, dest=Path(args.directory))
        else:
            with sessions() as session:
                restore(neo4j=neo4j, session=session, landing=landing, src=Path(args.directory))
                session.commit()
    finally:
        neo4j.close()
        engine.dispose()


if __name__ == "__main__":
    main()
