"""Reconcile-CLI refusal paths (Gate 3b WP-3) — the guards fire BEFORE any store is touched.

The full-store PASS path is operator-run (``docs/runbooks/OPERATOR_SESSION.md`` §7) over real
dual-Neo4j; the instruments themselves are ``@given``-tested in
``tests/property/test_prop_reconciliation.py``. What must be pinned here is the fail-closed
ordering: a missing/identical diff target exits 2 WITHOUT constructing a single Neo4j client —
the same refuse-before-touch posture as the scheduled guard (ADR 0102 D3).
"""

from __future__ import annotations

import pytest

from worldmonitor.resolution import reconcile_cli
from worldmonitor.settings import Settings


def _fail_from_settings(*_a: object, **_k: object) -> object:
    raise AssertionError("no Neo4j client may be constructed on a refusal path")


def test_missing_diff_uri_refuses_with_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(neo4j_uri="bolt://live:7687")  # projection_diff_neo4j_uri defaults ""
    monkeypatch.setattr("worldmonitor.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "worldmonitor.graph.neo4j_client.Neo4jClient.from_settings", _fail_from_settings
    )
    monkeypatch.setattr("worldmonitor.graph.neo4j_client.Neo4jClient.connect", _fail_from_settings)
    assert reconcile_cli.main() == 2


def test_same_target_textual_fence_refuses_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        neo4j_uri="bolt://live:7687",
        projection_diff_enabled=True,
        projection_diff_neo4j_uri="bolt://live:7687",  # EXACT match -> the fence refuses
    )
    monkeypatch.setattr("worldmonitor.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "worldmonitor.graph.neo4j_client.Neo4jClient.from_settings", _fail_from_settings
    )
    monkeypatch.setattr("worldmonitor.graph.neo4j_client.Neo4jClient.connect", _fail_from_settings)
    assert reconcile_cli.main() == 2
