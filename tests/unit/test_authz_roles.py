"""Unit tests — Gate L1-b: the pure REST role-helper (``authz/roles.py``).

``principal_has_role`` mirrors ``mcp/auth.py``'s ``_has_graph_read_role`` defensive
``isinstance(roles, Mapping)`` check, but lives in the REST layer for the dedicated
``worldmonitor:llm`` role (GATE_L1_LLM_EGRESS_HARDENING_SPEC.md §3.2). ``mcp/auth.py``
itself stays FROZEN — this is a fresh, deliberate copy of the claim URN, not a shared
refactor.

RED TODAY:
    ModuleNotFoundError: No module named 'worldmonitor.authz.roles'
    (authz/roles.py does not exist yet — it is L1-b's own new module, item 5)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from worldmonitor.authz.oidc import Principal

# CONTRACT: triggers ModuleNotFoundError until the builder adds authz/roles.py (L1-b item 5).
from worldmonitor.authz.roles import (
    WM_LLM_ROLE,
    ZITADEL_PROJECT_ROLES_CLAIM,
    principal_has_role,
)


def _principal(claims: Mapping[str, Any]) -> Principal:
    """Build a real ``Principal`` (frozen dataclass, ``authz/oidc.py``) for the given claims."""
    return Principal(subject="test-subject", claims=claims)


# ── the two claim constants match the spec verbatim ─────────────────────────────────────


def test_constants_match_spec_verbatim() -> None:
    assert WM_LLM_ROLE == "worldmonitor:llm", (
        f"WM_LLM_ROLE must be 'worldmonitor:llm', got {WM_LLM_ROLE!r}"
    )
    assert ZITADEL_PROJECT_ROLES_CLAIM == "urn:zitadel:iam:org:project:roles", (
        "ZITADEL_PROJECT_ROLES_CLAIM must be 'urn:zitadel:iam:org:project:roles', "
        f"got {ZITADEL_PROJECT_ROLES_CLAIM!r}"
    )


# ── principal_has_role: the Mapping-containing-role case → True ─────────────────────────


def test_role_present_in_roles_mapping_returns_true() -> None:
    principal = _principal(
        {
            "sub": "test-subject",
            ZITADEL_PROJECT_ROLES_CLAIM: {WM_LLM_ROLE: {"orgId": "org-1"}},
        }
    )
    assert principal_has_role(principal, WM_LLM_ROLE) is True, (
        "a Mapping claim containing the role must yield True"
    )


# ── principal_has_role: a Mapping WITHOUT the role → False ──────────────────────────────


def test_role_absent_from_roles_mapping_returns_false() -> None:
    principal = _principal(
        {
            "sub": "test-subject",
            ZITADEL_PROJECT_ROLES_CLAIM: {"worldmonitor:graph-read": {"orgId": "org-1"}},
        }
    )
    assert principal_has_role(principal, WM_LLM_ROLE) is False, (
        "a Mapping claim NOT containing the role must yield False"
    )


def test_empty_roles_mapping_returns_false() -> None:
    principal = _principal({"sub": "test-subject", ZITADEL_PROJECT_ROLES_CLAIM: {}})
    assert principal_has_role(principal, WM_LLM_ROLE) is False, (
        "an empty Mapping claim must yield False"
    )


# ── principal_has_role: the claim key ABSENT entirely → False ───────────────────────────


def test_claim_key_entirely_absent_returns_false() -> None:
    principal = _principal({"sub": "test-subject"})
    assert principal_has_role(principal, WM_LLM_ROLE) is False, (
        "a principal with no roles claim at all must yield False (not raise)"
    )


# ── principal_has_role: claim present but NOT a Mapping → False (defensive, mirrors mcp/auth.py) ──


def test_claim_present_but_a_list_returns_false() -> None:
    principal = _principal({"sub": "test-subject", ZITADEL_PROJECT_ROLES_CLAIM: [WM_LLM_ROLE]})
    assert principal_has_role(principal, WM_LLM_ROLE) is False, (
        "a non-Mapping (list) roles claim must yield False, not raise or falsely match "
        "via `in` on a list — mirrors mcp/auth.py's defensive isinstance(..., Mapping) check"
    )


def test_claim_present_but_none_returns_false() -> None:
    principal = _principal({"sub": "test-subject", ZITADEL_PROJECT_ROLES_CLAIM: None})
    assert principal_has_role(principal, WM_LLM_ROLE) is False, (
        "a None roles claim must yield False, not raise"
    )


def test_claim_present_but_a_string_returns_false() -> None:
    # A string is technically iterable/`in`-able in Python; must NOT be mistaken for a Mapping
    # (e.g. `"worldmonitor:llm" in "some-worldmonitor:llm-substring"` would be a false positive
    # if the helper used bare `in` instead of an isinstance(..., Mapping) guard).
    principal = _principal(
        {"sub": "test-subject", ZITADEL_PROJECT_ROLES_CLAIM: f"contains {WM_LLM_ROLE} substring"}
    )
    assert principal_has_role(principal, WM_LLM_ROLE) is False, (
        "a string roles claim must yield False even if it contains the role name as a substring"
    )
