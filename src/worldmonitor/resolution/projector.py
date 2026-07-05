"""Fold engine — log-as-outbox projector (Gate 3a-i / ADR 0100).

Rebuilds the resolved graph from the Postgres statement + decision log (the Gate 2a
SoR spine, ADR 0099) into an ISOLATED, EPHEMERAL Neo4j target, reusing
:func:`graph.writer.write_entities` unchanged (no ftmg touched here).

DORMANT / ISOLATED POSTURE (ADR 0100 D4): this module is never wired into the driver,
settings, or compose profile in 3a-i.  It is exercised only by the Gate 3a-i test suite
against a test-container target.  The projector NEVER co-writes the live graph.

Design decisions (ADR 0100):
  D1  The log IS the outbox — a monotonic ``seq BIGINT IDENTITY`` column on both
      ``statement`` and ``decision`` gives the total consumption order; the projector
      checkpoints on ``seq`` and reads rows since a watermark (no separate outbox table).
  D2  GLOBAL-FOLD-IS-TRUTH — every entity-typed property value is rewritten through
      ``survivor_of`` (= ``resolve_durable`` over the FULL canonical_id_ledger), so the
      projected graph is strictly more resolved than the per-batch live graph.
  D3  B-1 = PROJECTOR-SIDE DEDUP — statement_id deduplication happens here; no
      UNIQUE(statement_id) constraint is added (append-only semantics preserved).
  D4  DORMANT / ISOLATED — no driver wiring, no settings flag, no compose profile.
  D5  MODULE PLACEMENT = ``resolution/projector.py`` — the fold is a resolution-domain
      re-run of the merge math from the persisted log.

Public surface (pinned by tests):
  :func:`reconstruct_entities` — pure fold (no DB, no Neo4j); unit-testable.
  :func:`project`              — end-to-end: read → fold → write → checkpoint.
  :class:`ProjectionResult`   — fold outcome dataclass.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from followthemoney import registry
from followthemoney.exc import InvalidData
from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import (
    CanonicalIdLedger,
    ContextClaimRecord,
    DecisionRecord,
    ProjectionCheckpoint,
    StatementRecord,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.anchors import set_anchor_claims
from worldmonitor.ontology.ftm import FtmEntity, get_model, make_entity
from worldmonitor.provenance.model import Provenance, stamp, stamp_witness_map


@dataclass
class ProjectionResult:
    """Counts from one projection run (ADR 0100 / fold-engine observability surface).

    Returned by :func:`project` for the test assertions and future metric emission.
    """

    entities_written: int
    """Number of FtM entities passed to ``write_entities`` this call."""
    last_statement_seq: int
    """The ``statement.seq`` watermark written to ``projection_checkpoint`` after this fold."""
    last_decision_seq: int
    """The ``decision.seq`` watermark written to ``projection_checkpoint`` after this fold."""
    statements_read: int
    """Number of statement rows read from the log before deduplication."""
    statements_deduped: int
    """Number of duplicate-``statement_id`` rows removed during deduplication (ADR 0100 D3)."""
    statements_refolded: int = 0
    """Rows actually folded this call (ADR 0101 A1 observability). ``full_rebuild`` ==
    ``statements_read``; incremental == the full re-read of each touched survivor's history
    (``>= len(delta rows)``, 0 on a 0-delta no-op). Does NOT change ``statements_read``."""
    context_claims_read: int = 0
    """Number of ``context_claim`` rows read from the log before deduplication/grouping this
    call (Gate P1 / ADR 0106 observability). Additive; default preserves every existing
    construction of this dataclass."""


def reconstruct_entities(
    statement_rows: list[StatementRecord],
    survivor_of: Callable[[str], str],
    context_claim_rows: Sequence[ContextClaimRecord] = (),
) -> list[FtmEntity]:
    """Pure fold: reconstruct FtM entities from statement rows (no DB, no Neo4j).

    This is the heart of the fold algorithm (ADR 0100 D3).  It is PURE —
    no database access, no Neo4j writes — so it is unit-testable in isolation.

    Steps (deterministic throughout):

    (i)   **DEDUP** rows on ``statement_id`` — content-addressed duplicates are
          byte-identical in the projected value; at-least-once re-delivery of the
          same statement must be a no-op (ADR 0100 D3 / P-FOLD-3 semantic).

    (ii)  **GROUP** by ``survivor_of(canonical_id)`` — the GLOBAL referent rewrite
          (ADR 0100 D2) that maps every merged-away canonical id onto its survivor.

    (iii) Per survivor group **build ONE FtM entity**:

          * ``schema`` = the group's uniform statement schema.
          * ``properties`` = ``prop → sorted set of values``, EXCLUDING ``prop == "id"``.
            For ENTITY-TYPED props (``schema.properties[prop].type == registry.entity``)
            each value is REWRITTEN through ``survivor_of`` (global referent rewrite, D2).
          * ``entity = make_entity({"id": survivor, "schema": schema, "properties": props})``.
          * **PROVENANCE** (G1, node level): representative = ``min(entity_id)`` in the group;
            ``Provenance(source_id=dataset, retrieved_at=..., reliability=..., source_record=...)``
            from that member's rows; ``stamp()`` it.  ``dataset`` is NOT NULL (ADR 0099
            constraint) → ``provenance_node_properties`` is always non-empty → ``write_entities``
            never raises ``NodeProvenanceError`` (G1 upheld).
          * **WITNESS MAP** (Tier-1, ADR 0045): ``{prop: {row.dataset for rows of that prop}}``,
            EXCLUDING ``prop == "id"``; ``stamp_witness_map()`` it.  Re-derives exactly like
            :func:`resolution.merge._witness_map_from_statements`.
          * **ANCHORS**: reconstructed from the ``context_claim`` lane (Gate P1 / ADR 0106) —
            ``context_claim_rows`` are grouped by ``survivor_of(row.canonical_id)``, and for each
            distinct ``key`` the SET of claimed values is set via
            :func:`~worldmonitor.ontology.anchors.set_anchor_claims` (mirrors the
            ``merge_context`` union shape) BEFORE the provenance/witness stamping below, so
            :func:`~worldmonitor.ontology.anchors.get_anchors` applies the IDENTICAL
            omit-on-conflict rule as the live merged-entity path. A survivor with NO statement
            rows never gets a group here (this function groups by statement rows only) — a
            context-claim-only survivor therefore yields no entity and no anchors, a graceful
            no-op (dormant until Gate P3 wires the sign-off statement/decision spine).

    :param statement_rows:     Raw statement rows from the log (may contain duplicates).
    :param survivor_of:        Maps a canonical_id to its surviving durable id (identity if
                                the id has no alias row in the ledger).
    :param context_claim_rows: Raw ``context_claim`` rows (Gate P1 / ADR 0106); default empty —
                                every existing caller stays byte-behaviour-identical.
    :returns: One :class:`FtmEntity` per distinct survivor group (non-empty).
    """
    ftm_model = get_model()

    # Group context-claim rows by survivor (Gate P1 / ADR 0106) — read once, applied per group.
    context_by_survivor: dict[str, dict[str, set[str]]] = {}
    for claim_row in context_claim_rows:
        key_map = context_by_survivor.setdefault(survivor_of(claim_row.canonical_id), {})
        key_map.setdefault(claim_row.key, set()).add(claim_row.value)

    # (i) Dedup on statement_id — content-addressed duplicates are byte-identical
    seen_stmt_ids: set[str] = set()
    deduped_rows: list[StatementRecord] = []
    for row in statement_rows:
        if row.statement_id not in seen_stmt_ids:
            seen_stmt_ids.add(row.statement_id)
            deduped_rows.append(row)

    # (ii) Group by survivor subject (global canonical fold, ADR 0100 D2)
    groups: dict[str, list[StatementRecord]] = defaultdict(list)
    for row in deduped_rows:
        survivor = survivor_of(row.canonical_id)
        groups[survivor].append(row)

    entities: list[FtmEntity] = []

    for survivor, rows in groups.items():
        if not rows:
            continue

        # Schema = the FtM COMMON schema of the group's member schemas (F1, ADR 0100).
        # Mirrors the merge path (``proxy.merge`` sets ``schema = common_schema`` over the
        # members); using an arbitrary ``rows[0].schema`` would pick a sub/supertype by
        # unordered row order on a group whose members carry different-but-compatible schemas
        # (e.g. Company + Organization) → a divergent, potentially lossy node.
        schemata = sorted({row.schema for row in rows})
        schema_obj = ftm_model.get(schemata[0])
        for _other in schemata[1:]:
            if schema_obj is None:
                break
            try:
                schema_obj = ftm_model.common_schema(schema_obj, _other)
            except InvalidData:
                # Incompatible schemata cannot co-merge into one canonical (H-2, ADR 0041),
                # so this is unreachable for a real fold; fall back deterministically.
                break
        schema_name = schema_obj.name if schema_obj is not None else schemata[0]

        # Build props: {prop -> set of values}, EXCLUDING prop == "id"
        props: dict[str, set[str]] = defaultdict(set)
        # Witness map: {prop -> set of datasets}, EXCLUDING prop == "id"
        witnesses: dict[str, set[str]] = defaultdict(set)

        for row in rows:
            if row.prop == "id":
                continue  # exclude id pseudo-property (ADR 0100 D3)
            value = row.value
            # Entity-typed property: rewrite value through survivor_of (D2 global referent rewrite)
            # Same test as resolution.referents.rewrite_referents uses.
            if schema_obj is not None:
                prop_def = schema_obj.properties.get(row.prop)
                if prop_def is not None and prop_def.type == registry.entity:
                    value = survivor_of(value)
            props[row.prop].add(value)
            witnesses[row.prop].add(row.dataset)

        # Build the FtM entity (sorted values for determinism)
        sorted_props: dict[str, list[str]] = {p: sorted(v) for p, v in props.items()}
        entity = make_entity(
            {
                "id": survivor,
                "schema": schema_name,
                "properties": sorted_props,
            }
        )

        # Populate datasets from statement rows so proxy.datasets matches the
        # StatementEntity path (transform.py: list(proxy.datasets) → "datasets" node prop).
        # ValueEntity.datasets is a plain set[str] that can be assigned directly.
        entity.datasets = {row.dataset for row in rows if row.prop != "id"}

        # ANCHORS (Gate P1 / ADR 0106): reconstruct from the context_claim lane BEFORE the
        # provenance/witness stamping below — sets the RAW multi-value union per key so
        # get_anchors applies the identical omit-on-conflict rule as the live merged-entity path.
        for anchor_key, anchor_values in context_by_survivor.get(survivor, {}).items():
            set_anchor_claims(entity, anchor_key, anchor_values)

        # Provenance (G1): representative = member with min(entity_id) in the group
        rep_entity_id = min(row.entity_id for row in rows)
        rep_rows = [row for row in rows if row.entity_id == rep_entity_id]
        rep_row = rep_rows[0]  # any row from the representative member

        prov = Provenance(
            source_id=rep_row.dataset,
            retrieved_at=rep_row.retrieved_at or "",
            reliability=rep_row.reliability or "",
            source_record=rep_row.raw_pointer or "",
        )
        stamp(entity, prov)

        # Witness map (Tier-1, ADR 0045): {prop -> set of datasets}
        if witnesses:
            stamp_witness_map(entity, dict(witnesses))

        entities.append(entity)

    return entities


# The default checkpoint id (= the projection target name, e.g. "neo4j")
_TARGET_ID = "neo4j"


def _load_alias_map(session: Session) -> dict[str, str]:
    """The SUPERSESSION-only ``canonical_alias -> canonical_id`` map (ADR 0100 D2 / F2).

    One query over the complete ``canonical_id_ledger``, avoiding N per-row round-trips. F2
    (determinism): build the map from SUPERSESSION rows ONLY (``canonical_id != canonical_alias``)
    and read them in a deterministic ``ORDER BY``. Excluding self-rows (``canonical == alias``)
    is load-bearing: an id that is BOTH a live canonical (its self-row) AND later superseded (an
    alias row -> its survivor) must resolve to the SURVIVOR, not to itself by unordered last-wins.
    Otherwise a fresh Postgres / DR rebuild (the scenario the projector exists for) could flip the
    row order and leave an orphan node under the superseded id — the exact ADR-0095
    fold-under-re-canonicalisation guarantee this projector claims to enforce.
    """
    ledger_rows = session.execute(
        select(CanonicalIdLedger.canonical_alias, CanonicalIdLedger.canonical_id).order_by(
            CanonicalIdLedger.canonical_alias, CanonicalIdLedger.canonical_id
        )
    ).all()
    return {
        str(alias): str(canonical)
        for alias, canonical in ledger_rows
        if str(alias) != str(canonical)
    }


def build_survivor_of(session: Session) -> Callable[[str], str]:
    """Build the transitive ``survivor_of`` resolver over the FULL canonical_id_ledger.

    A pure extraction (ADR 0102 D9) of the F2 deterministic supersession-only ledger read +
    the cycle-guarded fixed-point walk :func:`project` uses to fold the log. Exported so the
    projection rebuild-and-diff guard (``runner.driver``) can apply the IDENTICAL referent
    semantics as the fold when measuring divergence — no second, drifting implementation.
    :func:`worldmonitor.resolution.canonical.resolve_durable` is single-hop/per-alias and is
    NOT usable here (the transitive walk below is required).
    """
    alias_map = _load_alias_map(session)

    def survivor_of(cid: str) -> str:
        """Resolve a (possibly superseded) canonical_id to its durable survivor.

        Follows the supersession chain transitively (a → b → c) to a fixed point, with a
        visited-guard against a pathological cycle. Deterministic: the map holds supersession
        rows only, so a coexisting self-row never shadows the survivor mapping.
        """
        seen: set[str] = set()
        current = cid
        while current in alias_map and current not in seen:
            seen.add(current)
            current = alias_map[current]
        return current

    return survivor_of


def project(
    session: Session,
    target: Neo4jClient,
    *,
    full_rebuild: bool = False,
    checkpoint_id: str = _TARGET_ID,
) -> ProjectionResult:
    """Fold the statement + decision log into the isolated ``target`` Neo4j.

    READ → FOLD → WRITE → CHECKPOINT (at-least-once ordering: Neo4j FIRST, then commit).

    :param session:       SQLAlchemy session (caller owns it; this function commits it).
    :param target:        The ISOLATED Neo4j target (never the live graph in 3a-i).
    :param full_rebuild:  If ``True``, read ALL rows regardless of the current watermark
                          and advance the checkpoint to ``max(seq)`` consumed.  Two
                          consecutive ``full_rebuild`` calls followed by an incremental
                          ``project(full_rebuild=False)`` must return ``statements_read == 0``
                          (P-FOLD-3, ADR 0100 D1).
    :param checkpoint_id: The :class:`ProjectionCheckpoint` row id to read/upsert (ADR 0102 D5).
                          Defaults to the module constant ``_TARGET_ID`` ("neo4j"), keeping
                          every existing caller byte-behaviour-identical. The projection
                          rebuild-and-diff guard passes ``"projection-diff"`` — a SEPARATE
                          row — so its full-rebuild fold NEVER advances the live projector's
                          own watermark.
    :returns: :class:`ProjectionResult` with counts for the tests + future observability.
    """
    # --- Read current checkpoint ---
    checkpoint = session.execute(
        select(ProjectionCheckpoint).where(ProjectionCheckpoint.id == checkpoint_id)
    ).scalar_one_or_none()

    if checkpoint is None or full_rebuild:
        last_stmt_seq: int = 0
        last_dec_seq: int = 0
        last_ctx_seq: int = 0
    else:
        last_stmt_seq = int(checkpoint.last_statement_seq)
        last_dec_seq = int(checkpoint.last_decision_seq)
        last_ctx_seq = int(checkpoint.last_context_claim_seq)

    # --- Read statement rows (ORDER BY seq for determinism) ---
    stmt_query = select(StatementRecord).order_by(StatementRecord.seq)
    if not full_rebuild and last_stmt_seq > 0:
        stmt_query = stmt_query.where(StatementRecord.seq > last_stmt_seq)
    statement_rows = list(session.execute(stmt_query).scalars().all())

    # --- Read decision rows ---
    dec_query = select(DecisionRecord).order_by(DecisionRecord.seq)
    if not full_rebuild and last_dec_seq > 0:
        dec_query = dec_query.where(DecisionRecord.seq > last_dec_seq)
    decision_rows = list(session.execute(dec_query).scalars().all())

    # --- Read context-claim rows (Gate P1 / ADR 0106) — same full/incremental shape ---
    ctx_query = select(ContextClaimRecord).order_by(ContextClaimRecord.seq)
    if not full_rebuild and last_ctx_seq > 0:
        ctx_query = ctx_query.where(ContextClaimRecord.seq > last_ctx_seq)
    context_claim_delta_rows = list(session.execute(ctx_query).scalars().all())

    statements_read = len(statement_rows)
    context_claims_read = len(context_claim_delta_rows)

    # Count statement_id deduplication (ADR 0100 D3 / P-FOLD-4)
    unique_stmt_ids = len({row.statement_id for row in statement_rows})
    statements_deduped = statements_read - unique_stmt_ids

    # --- Build survivor_of (ADR 0100 D2 global-fold-is-truth; extracted, ADR 0102 D9) ---
    survivor_of = build_survivor_of(session)

    # --- Determine the rows to fold ---
    # full_rebuild folds every row (both lanes). Incremental (F3 fix, ADR 0101 A1, extended to
    # the context-claim lane by Gate P1 / ADR 0106 §2.b.2): fold each TOUCHED survivor's
    # COMPLETE statement AND context-claim history — not just the delta — so the additive
    # `SET n += props` write restores full multi-valued props/anchors (a thinner delta re-emit
    # of an already-accumulated value would otherwise clobber it down to the last batch's
    # values). The touched set is the UNION of the statement delta AND the context-claim delta:
    # a survivor touched ONLY by a context-claim delta still needs its statement history
    # re-read (else there is no entity to hang the anchors on — reconstruct_entities groups by
    # statement rows).
    if full_rebuild:
        fold_rows = statement_rows
        fold_context_rows = context_claim_delta_rows
    else:
        touched = {survivor_of(str(row.canonical_id)) for row in statement_rows} | {
            survivor_of(str(row.canonical_id)) for row in context_claim_delta_rows
        }
        if not touched:
            fold_rows = []  # 0-delta no-op (preserves P-FOLD-3 statements_read==0)
            fold_context_rows = []
        else:
            # every canonical_id (survivor OR superseded alias) that folds into a touched survivor
            alias_map = _load_alias_map(session)
            preimage = set(touched) | {
                alias for alias in alias_map if survivor_of(alias) in touched
            }
            fold_rows = list(
                session.execute(
                    select(StatementRecord)
                    .where(StatementRecord.canonical_id.in_(preimage))
                    .order_by(StatementRecord.seq)
                )
                .scalars()
                .all()
            )
            fold_context_rows = list(
                session.execute(
                    select(ContextClaimRecord)
                    .where(ContextClaimRecord.canonical_id.in_(preimage))
                    .order_by(ContextClaimRecord.seq)
                )
                .scalars()
                .all()
            )

    # --- Fold: reconstruct entities from the statement log (+ anchors from the context lane) ---
    entities = reconstruct_entities(fold_rows, survivor_of, context_claim_rows=fold_context_rows)

    # --- Compute max seqs consumed (default = existing watermark so it never goes backward) ---
    max_stmt_seq = max((int(row.seq) for row in statement_rows), default=last_stmt_seq)
    max_dec_seq = max((int(row.seq) for row in decision_rows), default=last_dec_seq)
    max_ctx_seq = max((int(row.seq) for row in context_claim_delta_rows), default=last_ctx_seq)

    # --- Write Neo4j FIRST (at-least-once: idempotent MERGE before watermark commit) ---
    # A crash here leaves the watermark unmoved → the same delta is re-read and re-projected
    # on the next call (idempotent MERGE guarantees convergence, ADR 0100 D1).
    write_entities(target, entities)

    # --- Upsert checkpoint (watermark advances ONLY after Neo4j write succeeds) ---
    if checkpoint is None:
        session.add(
            ProjectionCheckpoint(
                id=checkpoint_id,
                last_statement_seq=max_stmt_seq,
                last_decision_seq=max_dec_seq,
                last_context_claim_seq=max_ctx_seq,
            )
        )
    else:
        checkpoint.last_statement_seq = max_stmt_seq
        checkpoint.last_decision_seq = max_dec_seq
        checkpoint.last_context_claim_seq = max_ctx_seq
        checkpoint.updated_at = datetime.now(UTC)

    session.commit()

    return ProjectionResult(
        entities_written=len(entities),
        last_statement_seq=max_stmt_seq,
        last_decision_seq=max_dec_seq,
        statements_read=statements_read,
        statements_deduped=statements_deduped,
        statements_refolded=len(fold_rows),
        context_claims_read=context_claims_read,
    )
