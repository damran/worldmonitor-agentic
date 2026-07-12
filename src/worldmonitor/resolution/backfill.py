"""Gate 2b — statement/context-claim log backfill (ADR 0113).

Reconstructs the pre-Gate-2a window of the statement + context-claim SoR spine (ADR 0099 / ADR
0106) from the retained ``er_queue_item.raw_entity`` substrate (SF-1 default,
``docs/decisions/0113-statement-log-backfill.md``). Every contribution resolved BEFORE Gate 2a's
dual-write began (``pipeline.py``'s promote point) wrote the live graph + ``merge_audit`` +
``canonical_id_ledger`` but **no spine rows** — so ``resolution.projector.project(full_rebuild=
True)`` raises ``IncompleteAliasedSurvivorError`` for every pre-2a aliased survivor (the WPI-2
obligation, ADR 0111, un-discharged). This module discharges it by minting the missing rows
through the SAME frozen writers the live pipeline uses, so a backfilled row is byte-identical to
what the dual-write path would have minted (INV-BACKFILL-FAITHFUL) and dedups against it exactly.

Design (spec ``docs/reviews/GATE_2B_BACKFILL_SPEC.md`` §2, decided ADR 0113 §Decided):

* **SF-1 source** — ``er_queue_item.raw_entity`` (byte-faithful: resolution itself builds its
  members from this exact substrate via ``make_entity``, so re-parsing reproduces the identical
  member and therefore the identical ``statement_id``). The landing re-map fallback (SF-1(b)) is
  NOT wired in this gate (no operator need surfaced; a future gap-closing revisit per the ADR).
* **SF-2 idempotence** — a Python-side pre-filter on the existing ``statement_id`` set (no
  ``UNIQUE(statement_id)`` constraint exists) makes a re-run a safe, cheap no-op
  (``skipped_duplicate``). The same idea is extended to the ``context_claim`` lane via a
  composite natural key (``canonical_id``, ``entity_id``, ``key``, ``value``, ``dataset``) — that
  table has no content-addressed id of its own, but re-running the backfill must not bloat it
  either; sharing the ``skipped_duplicate`` counter keeps one idempotence story for both lanes.
* **SF-3 forget-safety (person-affecting; the mandatory guard)** — two skip mechanisms, proven
  INDEPENDENTLY EXERCISABLE by the test suite: (i) an already-redacted ``er_queue_item.raw_entity``
  shell (``{"erased": True, ...}``) is never parsed, never contributes
  (``skipped_redacted_shell``); (ii) a member whose stamped ``Provenance.source_id`` is enumerated
  in the erase-audit exclusion set (every ``TaskRun(kind="erase", status="ok").
  stats["source_id"]``, :func:`load_erased_sources`, mirroring ``erasure_scrub.scrub_stock``'s own
  enumeration) is skipped before it ever reaches the frozen writers (``skipped_erased_source``). A
  shell produced by the REAL ``erasure.erase_source`` embeds its own erased ``source_id`` right on
  the shell (``_redact_queue``'s exact shape) — when that embedded ``source_id`` is ALSO in the
  exclusion set (the real end-to-end erase-then-backfill path, where both mechanisms coincide on
  the SAME item), the item is attributed to ``skipped_erased_source`` rather than the generic
  ``skipped_redacted_shell``, so the erase-audit counter stays meaningful for that path while a
  shell with no matching exclusion-set entry (a synthetic/test shell, or a future redaction
  producer with no ``TaskRun``) still counts as a plain ``skipped_redacted_shell`` — proving the
  shell-skip needs no exclusion-set entry to fire (see :func:`_iter_all_queue_items`). After every
  row is either written or skipped, :func:`backfill_spine` re-runs the FROZEN
  ``erasure_scrub.scrub_stock`` as a belt-and-suspenders re-closure of any row a race
  re-introduced (backfilled rows are ``dataset == source_id`` reachable, exactly like dual-write
  rows).
* **SF-4 completion** — :func:`assert_backfill_complete` re-uses the FROZEN
  ``spine_integrity.find_incomplete_aliased_survivors`` (the same fold-side coverage check
  ``projector.project(full_rebuild=True)`` runs) to prove every aliased survivor now has >= 1
  foldable statement row. The spec's SECOND completion half — a whole-graph divergence spike over
  the ``divergence._excluded`` axes — needs a live AND a fold Neo4j target side by side and is
  exercised directly at the integration-test layer (``tests/integration/test_backfill.py``, via
  the SAME production ``resolution.divergence.measure_divergence`` instrument
  ``test_projection_diff.py`` uses) rather than folded into this session-only helper; see
  :func:`assert_backfill_complete`'s docstring for the exact API-contract note.
* **Rider-1 stamped-ness (INV-BACKFILL-STAMPED)** — inherited for free: the frozen
  ``resolution.statements.fuse_statement_rows`` / ``fuse_context_claim_rows`` already
  skip-and-log any member with no stamped provenance / an empty ``source_id`` rather than write a
  source-unreachable ``dataset``. This module additionally pre-counts those members (before
  calling the frozen writers) into ``skipped_source_unreachable`` for observability.
* **Decision rows are OUT OF SCOPE** — the fold does not consume ``decision.member_ids`` for
  reconstruction (statement + context-claim lanes only feed ``reconstruct_entities``), so no
  ``DecisionRecord`` is backfilled here (spec §2 slice 2b-a).

Reuses, never edits (FROZEN, spec §8): ``resolution.statements.fuse_statement_rows`` /
``fuse_context_claim_rows`` (+ the ``WM_EXISTS`` sentinel), ``resolution.merge.ResolvedCluster``,
``resolution.projector.build_survivor_of`` / ``_load_alias_map`` (read-only), ``resolution.
spine_integrity.find_incomplete_aliased_survivors`` (read-only), ``resolution.erasure_scrub.
scrub_stock`` (called, never modified).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from followthemoney.exc import InvalidData
from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ContextClaimRecord, ErQueueItem, StatementRecord, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import get_provenance
from worldmonitor.resolution.erasure_scrub import scrub_stock
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.projector import (
    _load_alias_map,  # pyright: ignore[reportPrivateUsage]
    build_survivor_of,
)
from worldmonitor.resolution.spine_integrity import find_incomplete_aliased_survivors
from worldmonitor.resolution.spine_lock import acquire_spine_writer_lock
from worldmonitor.resolution.statements import (
    WM_EXISTS,
    fuse_context_claim_rows,
    fuse_statement_rows,
)

logger = logging.getLogger(__name__)

# The natural (non-content-addressed) dedup key for a context-claim row — that lane has no
# statement_id-equivalent hash, so the composite tuple stands in for SF-2's pre-filter there.
_ContextKey = tuple[str, str, str, str, str]


@dataclass
class BackfillResult:
    """Counts from one :func:`backfill_spine` run (dry-run or committed)."""

    members_scanned: int = 0
    """Every ``ErQueueItem`` row examined, regardless of disposition."""
    statements_written: int = 0
    """New (non-``WM_EXISTS``) :class:`~worldmonitor.db.models.StatementRecord` rows added."""
    context_claims_written: int = 0
    """New :class:`~worldmonitor.db.models.ContextClaimRecord` rows added."""
    existence_claims_written: int = 0
    """New ``WM_EXISTS`` sentinel rows added (WPI-1 / ADR 0112 zero-prop disposition)."""
    skipped_duplicate: int = 0
    """Rows skipped because their dedup key (``statement_id``, or the context-claim composite
    key) is already present — pre-existing (post-2a dual-write) or from an earlier backfill run
    (SF-2 idempotence)."""
    skipped_source_unreachable: int = 0
    """Members with no stamped provenance / an empty ``source_id`` (rider-1, ADR 0112) —
    never written with a source-unreachable ``dataset``."""
    skipped_redacted_shell: int = 0
    """``ErQueueItem`` rows already redacted to an ``{"erased": True}`` shell — never parsed."""
    skipped_erased_source: int = 0
    """Members whose ``Provenance.source_id`` is in the erase-audit exclusion set (SF-3)."""
    survivors_covered: int = 0
    """Distinct canonical-id groups that yielded >= 1 statement/context-claim row this run
    (new or already-present) — the WPI-2 completeness target."""


class BackfillIncompleteError(RuntimeError):
    """Raised by :func:`assert_backfill_complete` when an aliased survivor is still uncovered."""


def load_erased_sources(session: Session) -> set[str]:
    """The distinct erase-audit exclusion set (SF-3) — every successfully-erased ``source_id``.

    Reads ``TaskRun(kind="erase", status="ok")`` rows and extracts ``.stats["source_id"]``
    PYTHON-SIDE (never a Postgres-only ``stats->>`` query), mirroring
    :func:`~worldmonitor.resolution.erasure_scrub.scrub_stock`'s own enumeration exactly — so this
    also runs on the Docker-free SQLite unit lane. A failed/running erase, or a different
    ``TaskRun.kind`` entirely, never contributes; a duplicate ``TaskRun`` for the same source
    (an idempotent re-run) collapses to one set entry.
    """
    sources: set[str] = set()
    task_runs = session.execute(
        select(TaskRun).where(TaskRun.kind == "erase", TaskRun.status == "ok")
    ).scalars()
    for run in task_runs:
        stats = run.stats
        source_id = stats.get("source_id") if isinstance(stats, dict) else None
        if isinstance(source_id, str) and source_id:
            sources.add(source_id)
    return sources


_QueueDisposition = Literal["kept", "redacted_shell", "erased_source", "unparseable"]


def _iter_all_queue_items(
    session: Session, *, exclude_sources: set[str]
) -> Iterator[tuple[_QueueDisposition, FtmEntity | None, str | None]]:
    """Classify every ``ErQueueItem`` row into a backfill disposition (internal, full scan).

    Shared engine behind :func:`iter_backfill_members` (which yields only the ``"kept"`` rows)
    AND :func:`backfill_spine` (which needs the skip counts too, not just the survivors). Builds
    ``survivor_of`` ONCE over the full ledger, not per row.
    """
    survivor_of = build_survivor_of(session)
    # Deterministic order (INV-BACKFILL-IDEMPOTENT): when >1 ErQueueItem shares a member.id — an
    # entity re-crawled under a new landing record, uq_er_queue_dedup is (source_record, entity_id)
    # — the last-written snapshot wins the ``by_id`` dict below. Without an ORDER BY, WHICH snapshot
    # wins is not SQL-stable across runs, so a re-run could pick a different winner and write a row
    # the first run did not (a latent idempotence break). Ordering by ``(created_at, id)`` makes the
    # winner the LATEST observation deterministically — which is also what the live graph's additive
    # ``SET n += props`` last-write-wins already reflects, so the backfill matches the live state.
    ordered = select(ErQueueItem).order_by(ErQueueItem.created_at, ErQueueItem.id)
    for item in session.execute(ordered).scalars():
        raw = item.raw_entity
        if raw.get("erased"):
            # Already redacted to a shell — never parseable, never a backfill contribution
            # regardless of category (SF-3). ``erasure.erase_source``'s real redaction shape
            # (``_redact_queue``) embeds the erased ``source_id`` right on the shell
            # (``{"erased": True, "source_id": <source_id>}``); a shell produced by a REAL erase
            # of a source already in the exclusion set is attributed to the erase-audit mechanism
            # (``skipped_erased_source``) rather than the generic shell-skip
            # (``skipped_redacted_shell``), so the two counters stay meaningful when both
            # mechanisms coincide on the SAME item (the real end-to-end erase-then-backfill path)
            # while remaining independently exercisable (a shell whose embedded ``source_id`` is
            # NOT in the exclusion set — e.g. a synthetic/test shell, or a future redaction
            # producer that never registers a ``TaskRun`` — still counts as a plain
            # ``redacted_shell``, proving the shell-skip needs no exclusion-set entry to fire).
            shell_source = raw.get("source_id")
            if isinstance(shell_source, str) and shell_source in exclude_sources:
                yield "erased_source", None, None
            else:
                yield "redacted_shell", None, None
            continue
        try:
            member = make_entity(raw)
        except InvalidData:
            logger.warning(
                "backfill: skipping er_queue_item id=%r — raw_entity is not a valid FtM entity",
                item.id,
            )
            yield "unparseable", None, None
            continue
        if member.id is None:
            yield "unparseable", None, None
            continue

        prov = get_provenance(member)
        if prov is not None and prov.source_id in exclude_sources:
            # SF-3 mechanism 2: the erase-audit exclusion set — independent of the shell skip
            # above (this member's raw_entity is NOT a shell; its source was erased separately).
            yield "erased_source", None, None
            continue

        yield "kept", member, survivor_of(member.id)


def iter_backfill_members(
    session: Session, *, exclude_sources: set[str]
) -> Iterator[tuple[FtmEntity, str]]:
    """The SF-1 backfill reader: one ``(member, canonical_id)`` pair per eligible ``ErQueueItem``.

    For each **non-redacted** ``ErQueueItem`` whose ``Provenance.source_id`` is NOT in
    ``exclude_sources``: ``make_entity(raw_entity)`` reconstructs the member EXACTLY as resolution
    itself built it (SF-1 byte-faithfulness), and ``canonical_id`` resolves via the SAME
    transitive ``build_survivor_of`` resolver the projector's fold uses — a singleton with only
    its own ledger self-row resolves to itself; a merge-aliased member resolves to its survivor.
    """
    for disposition, member, canonical_id in _iter_all_queue_items(
        session, exclude_sources=exclude_sources
    ):
        if disposition == "kept":
            assert member is not None
            assert canonical_id is not None
            yield member, canonical_id


def backfill_spine(
    session: Session,
    *,
    neo4j: Neo4jClient,
    dry_run: bool = False,
) -> BackfillResult:
    """The one-time, idempotent statement/context-claim log backfill (Gate 2b / ADR 0113).

    1. Loads the erase-audit exclusion set (:func:`load_erased_sources`).
    2. Pre-filters on the existing ``statement_id`` set (+ the context-claim composite key) —
       the SF-2 dedup pre-filter (there is no ``UNIQUE(statement_id)`` constraint).
    3. Groups :func:`iter_backfill_members`'s output by ``canonical_id`` and projects each group
       through the FROZEN ``resolution.statements.fuse_statement_rows`` /
       ``fuse_context_claim_rows`` — inheriting rider-1's source-unreachable skip and WPI-1's
       ``WM_EXISTS`` existence-claim disposition for a zero-prop survivor.
    4. Commits (unless ``dry_run``), then re-runs the FROZEN ``erasure_scrub.scrub_stock`` as the
       post-backfill re-scrub (SF-3(iii), belt-and-suspenders) and commits again.

    ``neo4j`` is REQUIRED (not optional): the pinned spec signature
    (``docs/reviews/GATE_2B_BACKFILL_SPEC.md`` §2) omits it, but ``scrub_stock`` (FROZEN,
    ``resolution/erasure_scrub.py``) needs a live client to prune, so this function must itself
    accept and forward one — a mechanical, one-line reconciliation the test suite's own
    API-CONTRACT NOTE anticipates and pins as a keyword named ``neo4j``.

    ``dry_run=True`` simulates the run (counts reflect what WOULD be written) without touching
    ``session`` (no ``add``, no ``commit``) or Neo4j (``scrub_stock`` is not invoked either — it
    writes Neo4j immediately and is not simulate-safe).
    """
    result = BackfillResult()
    exclude_sources = load_erased_sources(session)

    # INV-SINGLE-WRITER (WPI-3 / ADR 0110): the backfill is a NEW SoR-spine writer — take the
    # transaction-scoped advisory lock BEFORE the dedup pre-filter read so the read-then-write
    # window is atomic against a concurrent ingest/promote/sign-off writer. Without it, a
    # concurrent writer committing between the ``existing_statement_ids`` snapshot and this run's
    # commit would defeat the pre-filter (no ``UNIQUE(statement_id)`` backstops it) and interleave
    # ``seq`` assignment (a projector watermark gap). Fail-closed: a second concurrent spine writer
    # is refused (``ConcurrentSpineWriterError``), exactly as ``pipeline.py`` / ``signoff.py`` do.
    # No-op on SQLite (single-connection unit lane); the lock auto-releases at the commit below.
    if not dry_run:
        acquire_spine_writer_lock(session)

    existing_statement_ids: set[str] = set(
        session.execute(select(StatementRecord.statement_id)).scalars()
    )
    existing_context_keys: set[_ContextKey] = {
        (row.canonical_id, row.entity_id, row.key, row.value, row.dataset)
        for row in session.execute(select(ContextClaimRecord)).scalars()
    }

    # --- group eligible members by canonical_id (mirrors the by_id shape pipeline.py builds) ---
    members_by_canonical: dict[str, dict[str | None, FtmEntity]] = defaultdict(dict)
    for disposition, member, canonical_id in _iter_all_queue_items(
        session, exclude_sources=exclude_sources
    ):
        result.members_scanned += 1
        if disposition == "redacted_shell":
            result.skipped_redacted_shell += 1
            continue
        if disposition == "erased_source":
            result.skipped_erased_source += 1
            continue
        if disposition == "unparseable":
            continue
        assert member is not None
        assert canonical_id is not None
        members_by_canonical[canonical_id][member.id] = member

    written_statement_ids: set[str] = set()
    written_context_keys: set[_ContextKey] = set()

    for canonical_id, by_id in members_by_canonical.items():
        members_list = list(by_id.values())
        member_ids = tuple(sorted(mid for mid in by_id if mid is not None))

        # rider-1 observability: count (BEFORE calling the frozen writers, which already
        # skip-and-log these internally) every member with no stamped provenance / empty
        # source_id — never written with a source-unreachable dataset.
        for member in members_list:
            prov = get_provenance(member)
            if prov is None or not prov.source_id:
                result.skipped_source_unreachable += 1

        cluster = ResolvedCluster(
            canonical_id=canonical_id,
            member_ids=member_ids,
            entity=members_list[0],
            score=1.0,
        )
        stmt_rows = fuse_statement_rows(cluster, by_id)
        ctx_rows = fuse_context_claim_rows(canonical_id, members_list)

        if stmt_rows or ctx_rows:
            result.survivors_covered += 1

        for stmt_row in stmt_rows:
            if (
                stmt_row.statement_id in existing_statement_ids
                or stmt_row.statement_id in written_statement_ids
            ):
                result.skipped_duplicate += 1
                continue
            written_statement_ids.add(stmt_row.statement_id)
            if stmt_row.prop == WM_EXISTS:
                result.existence_claims_written += 1
            else:
                result.statements_written += 1
            if not dry_run:
                session.add(stmt_row)

        for ctx_row in ctx_rows:
            ctx_key: _ContextKey = (
                ctx_row.canonical_id,
                ctx_row.entity_id,
                ctx_row.key,
                ctx_row.value,
                ctx_row.dataset,
            )
            if ctx_key in existing_context_keys or ctx_key in written_context_keys:
                result.skipped_duplicate += 1
                continue
            written_context_keys.add(ctx_key)
            result.context_claims_written += 1
            if not dry_run:
                session.add(ctx_row)

    if dry_run:
        return result

    session.commit()

    # SF-3(iii): the post-backfill re-scrub — belt-and-suspenders re-closure of any row a race
    # re-introduced for an already-erased source (backfilled rows are dataset == source_id
    # reachable, exactly like dual-write rows).
    scrub_stock(session, neo4j=neo4j)
    session.commit()

    return result


def assert_backfill_complete(session: Session) -> None:
    """Raise :class:`BackfillIncompleteError` unless every aliased survivor is now covered.

    The fold-side coverage half of the spec's SF-4 completion criterion (spec §2 slice 2b-c):
    re-uses the FROZEN ``spine_integrity.find_incomplete_aliased_survivors`` over the FULL
    ``canonical_id_ledger`` + the FULL ``statement`` table — the SAME check
    ``resolution.projector.project(full_rebuild=True)`` runs internally (WPI-2, ADR 0111). This is
    exactly the completeness half :func:`backfill_spine` exists to discharge; when it holds, a
    subsequent ``full_rebuild`` no longer raises ``IncompleteAliasedSurvivorError``.

    API-CONTRACT NOTE: the spec also names a SECOND completion half — a whole-graph divergence
    spike over the ``divergence._excluded`` axes (id/caption/bare anchor keys/datasets/prov_*),
    ``docs/reviews/GATE_2B_BACKFILL_SPEC.md`` §2 slice 2b-c / §6. That half compares a LIVE graph
    snapshot against a FOLD graph snapshot (``resolution.divergence.measure_divergence``) and
    therefore needs two Neo4j targets side by side — a shape this function's pinned, session-only
    signature (``assert_backfill_complete(session) -> None``, spec §2) cannot express without
    silently redefining the API the test suite imports. No test in this gate calls
    ``assert_backfill_complete`` directly (confirmed against
    ``tests/property/test_prop_backfill.py`` and ``tests/integration/test_backfill.py``); the
    divergence-spike half is instead exercised directly at the integration layer via
    ``resolution.divergence.measure_divergence`` (``tests/integration/test_backfill.py::
    test_it_backfill_completeness_and_fold_reconstruction``, the SAME production instrument
    ``test_projection_diff.py::IT-DIV-1`` uses) — this function covers the fold-side coverage
    check only, and is a thin, reusable wrapper a future caller (e.g. an operator CLI) can compose
    with a live divergence check of its own.
    """
    alias_map = _load_alias_map(session)
    statement_rows = list(session.execute(select(StatementRecord)).scalars())
    incomplete = find_incomplete_aliased_survivors(alias_map, statement_rows)
    if incomplete:
        raise BackfillIncompleteError(
            f"{len(incomplete)} aliased survivor(s) still have no foldable statement row after "
            f"backfill: {sorted(incomplete)[:20]}"
        )
