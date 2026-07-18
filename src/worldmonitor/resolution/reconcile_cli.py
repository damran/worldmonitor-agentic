"""One-shot Gate 3b reconciliation runner — `python -m worldmonitor.resolution.reconcile_cli`.

The operator-facing wrapper the cutover runbook names (``docs/runbooks/OPERATOR_SESSION.md`` §7;
plan ``docs/fable-review/82_GATE_3B_CUTOVER_PLAN.md`` §4): it wires the PURE instruments in
:mod:`worldmonitor.resolution.reconciliation` (kept import-pure per INV-RECON-PURE — hence this
separate module) to the real stores:

1. **Fence + identity handshake** the ISOLATED diff target (the ``projection_diff_*`` settings),
   reusing the driver guard's own ``_same_neo4j_target`` / ``_database_id`` — never a second,
   driftable implementation. Refuses (exit 2) before touching anything when the target is not
   provably distinct from the live graph (ADR 0102 D3).
2. **Wipe + full-rebuild fold** into the diff target under the SEPARATE ``"reconciliation"``
   checkpoint (the live projector's watermark is never touched), sharing ONE ledger read across
   the fold, the WPI-2 completeness check, and every instrument below (Gate 3b LOW-1).
3. **Snapshot both graphs read-only** and run the instruments: R11/R11b count reconciliation,
   §3.1 label parity, R9b erased-source residue, R9c co-present value divergence.

Writes: ONLY the isolated diff target's wipe/fold and its own checkpoint row — the live graph and
every other table are read-only here.

Exit codes: 0 = ALL PASS · 1 = at least one instrument FAILED · 2 = refused/misconfigured.
FAIL conditions: non-zero node/edge residuals; label LOSS (``missing_in_fold`` — the executed
closure of the red-team topic-label CRITICAL); erased-source residue (the resurrection CRITICAL's
closure); co-present value divergence. ``extra_in_fold`` labels are reported but non-fatal
(§3.1's loss-direction-first posture).
"""

from __future__ import annotations

import sys


def main() -> int:
    # Store-touching imports live HERE, not at module top: this module is the impure shell; the
    # instruments module stays import-pure (INV-RECON-PURE).
    from worldmonitor.db.engine import engine_from_settings, session_factory
    from worldmonitor.graph.neo4j_client import Neo4jClient
    from worldmonitor.graph.snapshot import read_graph_snapshot
    from worldmonitor.resolution.backfill import load_erased_sources
    from worldmonitor.resolution.projector import load_alias_map_and_survivor_of, project
    from worldmonitor.resolution.reconciliation import (
        compare_labels,
        find_copresent_value_divergence,
        find_erased_source_residue,
        reconcile_counts,
    )
    from worldmonitor.runner.driver import (
        _database_id,  # pyright: ignore[reportPrivateUsage]
        _same_neo4j_target,  # pyright: ignore[reportPrivateUsage]
    )
    from worldmonitor.settings import get_settings

    settings = get_settings()
    if not settings.projection_diff_neo4j_uri:
        print(
            "REFUSED: projection_diff_neo4j_uri is not set — the reconciliation fold needs an "
            "ISOLATED second Neo4j (see docs/runbooks/OPERATOR_SESSION.md §6-§7).",
            file=sys.stderr,
        )
        return 2

    # Gate 1 — the textual fence, before any client construction (ADR 0102 D3).
    if _same_neo4j_target(settings.neo4j_uri, settings.projection_diff_neo4j_uri):
        print(
            "REFUSED: projection_diff_neo4j_uri resolves to the SAME Neo4j instance as the live "
            "neo4j_uri — refusing to wipe/fold (ADR 0102 D3).",
            file=sys.stderr,
        )
        return 2

    live = Neo4jClient.from_settings(settings)
    pw = settings.projection_diff_neo4j_password.get_secret_value()
    diff = Neo4jClient.connect(
        uri=settings.projection_diff_neo4j_uri,
        user=settings.projection_diff_neo4j_user,
        password=pw,
    )
    try:
        # Gate 2 — the authoritative identity handshake (read-only, before the wipe).
        live_id, diff_id = _database_id(live), _database_id(diff)
        if live_id is None or diff_id is None or live_id == diff_id:
            print(
                f"REFUSED: identity handshake (live id={live_id!r}, diff id={diff_id!r}) — the "
                "diff target is (or cannot be proven distinct from) the LIVE database "
                "(ADR 0102 D3).",
                file=sys.stderr,
            )
            return 2

        diff.execute_write("MATCH (n) DETACH DELETE n")
        sessions = session_factory(engine_from_settings(settings))
        with sessions() as session:
            alias_map, survivor_of = load_alias_map_and_survivor_of(session)
            project(
                session,
                diff,
                full_rebuild=True,
                checkpoint_id="reconciliation",
                survivor_of=survivor_of,
                alias_map=alias_map,
            )
            erased = frozenset(load_erased_sources(session))

        live_snap = read_graph_snapshot(live)
        fold_snap = read_graph_snapshot(diff)

        counts = reconcile_counts(live_snap, fold_snap, survivor_of)
        labels = compare_labels(live_snap, fold_snap, survivor_of)
        residue = find_erased_source_residue(fold_snap, erased)
        copresent = find_copresent_value_divergence(live_snap, fold_snap, survivor_of)

        checks = {
            "counts (R11/R11b residuals == 0)": (
                counts.node_residual == 0 and counts.edge_residual == 0
            ),
            "label parity (§3.1 — no LOSS)": not labels.missing_in_fold,
            "erased-source residue (R9b — none)": not residue,
            "co-present value divergence (R9c — none)": not copresent,
        }

        print(f"counts: {counts}")
        print(
            f"labels: missing_in_fold={len(labels.missing_in_fold)} "
            f"extra_in_fold={len(labels.extra_in_fold)} (extras reported, non-fatal)"
        )
        for finding in labels.missing_in_fold[:20]:
            print(f"  LOSS  node={finding.node_id} label={finding.label}")
        print(f"erased-source residue: {len(residue)}")
        for res in residue[:20]:
            print(f"  RESIDUE  node={res.node_id} source={res.source_id}")
        print(f"co-present value divergence: {len(copresent)}")
        for div in copresent[:20]:
            print(f"  DIVERGENCE  node={div.node_id} prop={div.prop} value={div.value!r}")

        all_pass = all(checks.values())
        for name, ok in checks.items():
            print(f"{'PASS' if ok else 'FAIL'}  {name}")
        print("RECONCILIATION: " + ("PASS" if all_pass else "FAIL"))
        return 0 if all_pass else 1
    finally:
        diff.close()
        live.close()


if __name__ == "__main__":  # pragma: no cover - thin CLI shell
    raise SystemExit(main())
