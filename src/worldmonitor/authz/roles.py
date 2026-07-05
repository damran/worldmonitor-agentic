"""REST-layer project-role gate for the dedicated ``worldmonitor:llm`` role.

Gate L1-b (ADR 0104 item 5, GATE_L1_LLM_EGRESS_HARDENING_SPEC.md §3.2). Zitadel surfaces
granted project roles under a reserved claim (a mapping role-name -> {orgId: ...}). MCP
already reads this claim for ``worldmonitor:graph-read`` in ``mcp/auth.py``
(``_has_graph_read_role``); this module is a fresh, deliberate copy of that same claim URN
for the REST layer's own ``worldmonitor:llm`` role — ``mcp/auth.py`` stays untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from worldmonitor.authz.oidc import Principal

# See the sibling definition in worldmonitor/mcp/auth.py (ZITADEL_ROLE_CLAIM) — a deliberate,
# separate copy for the REST layer; not shared/refactored (keeps MCP's auth module frozen).
ZITADEL_PROJECT_ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

WM_LLM_ROLE = "worldmonitor:llm"


def principal_has_role(principal: Principal, role: str) -> bool:
    """True iff ``principal``'s verified claims grant the given project ``role``.

    Mirrors ``mcp/auth.py``'s ``_has_graph_read_role`` defensive
    ``isinstance(roles, Mapping)`` check: the claim value is only trusted when it is itself a
    mapping (role-name -> {orgId: ...}); anything else (absent, ``None``, a list, a string)
    yields ``False`` rather than raising or false-matching via a bare ``in``.
    """
    roles: Any = principal.claims.get(ZITADEL_PROJECT_ROLES_CLAIM)
    return isinstance(roles, Mapping) and role in roles
