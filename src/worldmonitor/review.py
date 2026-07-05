"""CLI for reviewing parked sensitive/oversized merges (ADR 0031).

    python -m worldmonitor.review list
    python -m worldmonitor.review approve --canonical wmc-... --approver alice
    python -m worldmonitor.review reject  --canonical wmc-... --approver alice

(``--canonical`` is the parked merge's canonical id from ``list``; a merge's id is the
deterministic ``wmc-`` content id, ADR 0036.)

``--approver`` is the operator identity (a string in v0; Zitadel-backed in Phase 2);
``--reason`` is optional. The platform is single-tenant (D1, ADR 0042). The API/UI
surface is Phase 2; this is the v0 interface.
"""

from __future__ import annotations

import argparse
import logging

from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.resolution import signoff
from worldmonitor.settings import get_settings

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldmonitor.review",
        description="Review parked sensitive/oversized merges (ADR 0031).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list parked merges awaiting sign-off")
    for name, helptext in (
        ("approve", "promote a parked merge"),
        ("reject", "reject a parked merge (write its members as separate entities)"),
    ):
        action = sub.add_parser(name, help=helptext)
        action.add_argument("--canonical", required=True)
        action.add_argument("--approver", required=True)
        action.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = engine_from_settings(settings)
    sessions = session_factory(engine)
    neo4j = Neo4jClient.connect(
        uri=settings.neo4j_uri, user=settings.neo4j_user, password=settings.neo4j_password
    )
    try:
        if args.command == "list":
            # Pass neo4j so a half-committed sign-off (graph written, audit still pending —
            # the B-1 crash window) is surfaced as recoverable rather than silently stuck.
            with sessions() as session:
                parked = signoff.list_parked(session, neo4j)
            for merge in parked:
                logger.info(
                    "%s  reason=%s  members=%s  score=%.3f%s",
                    merge.canonical_id,
                    merge.reason,
                    list(merge.source_ids),
                    merge.score,
                    "  [GRAPH-WRITTEN: incomplete sign-off — re-run approve/reject to recover]"
                    if merge.graph_written
                    else "",
                )
            logger.info("%d parked merge(s)", len(parked))
            return 0

        decide = signoff.approve if args.command == "approve" else signoff.reject
        with sessions() as session:
            result = decide(
                session,
                neo4j,
                canonical_id=args.canonical,
                approver=args.approver,
                reason=args.reason,
            )
        if result.already_applied:
            logger.info(
                "%s %s: already applied (idempotent no-op)", result.decision, result.canonical_id
            )
        else:
            logger.info(
                "%s %s: wrote %d entity(ies), %d edge(s)",
                result.decision,
                result.canonical_id,
                result.entities_written,
                result.edges_written,
            )
        return 0
    finally:
        neo4j.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
