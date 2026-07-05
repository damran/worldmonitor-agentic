"""PRIMARY property test — Gate L1-b: LLM caller attribution + role-gate /v1.

This is the ``@given`` oracle for INV-CALLER + INV-ROLE (GATE_L1_LLM_EGRESS_HARDENING_SPEC.md
§3.5, ADR 0104 items 4-5).

  P-CALLER  An authenticated ``/v1`` request from a principal HOLDING ``worldmonitor:llm``
            reaches the gateway with ``caller_tag == principal.subject`` (falling back to
            ``"hermes"`` only when the subject is empty).
  P-ROLE    A valid token WITHOUT ``worldmonitor:llm`` → 403, gateway NEVER called; a valid
            token WITH it → 200, gateway called exactly once; a TOKENLESS request → 401
            (unauthenticated != forbidden — proves the role check composes on top of
            ``get_principal``'s existing auth gate, it does not replace/swallow it).

Reuses the ``_SpyGateway`` / ``TestClient`` / fake-verifier / ``_openai_body()`` patterns from
``tests/property/test_api_llm_gateway_delegation.py`` (replicated here per the test-author
brief — that file's own updates are a separate, narrower edit; see its history).

BUILDER CONTRACT — the implementation MUST match these names exactly:

    worldmonitor.authz.roles                          # NEW MODULE
        ZITADEL_PROJECT_ROLES_CLAIM: str = "urn:zitadel:iam:org:project:roles"
        WM_LLM_ROLE: str = "worldmonitor:llm"
        principal_has_role(principal, role) -> bool
    worldmonitor.api.deps
        require_llm_role(request: Request) -> Principal   # 401 unauth, 403 no-role, else principal
    worldmonitor.api.llm
        POST /v1/chat/completions gated on Depends(require_llm_role) (not bare get_principal)
        caller_tag = _principal.subject or "hermes"        # not the hardcoded "hermes"

RED TODAY:
    ModuleNotFoundError: No module named 'worldmonitor.authz.roles'
    (authz/roles.py does not exist yet; even once it exists, api/llm.py still hardcodes
    caller_tag="hermes" and gates on Depends(get_principal) with no role check, so
    P-CALLER fails on caller_tag mismatch and P-ROLE fails on 200-instead-of-403.)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError

# CONTRACT: triggers ModuleNotFoundError until the builder adds authz/roles.py (L1-b item 5).
from worldmonitor.authz.roles import (  # noqa: F401  # CONTRACT: roles module
    WM_LLM_ROLE,
    ZITADEL_PROJECT_ROLES_CLAIM,
)
from worldmonitor.settings import Settings

# ── canned fake ModelResponse (replicated from test_api_llm_gateway_delegation.py) ─────


class _FakeMessage:
    def __init__(self, content: str = "The answer is 42.", role: str = "assistant") -> None:
        self.content = content
        self.role = role


class _FakeChoice:
    def __init__(self) -> None:
        self.message = _FakeMessage()


class _FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 20
    total_tokens: int = 30


class _FakeModelResponse:
    def __init__(self) -> None:
        self.choices: list[_FakeChoice] = [_FakeChoice()]
        self.usage: _FakeUsage = _FakeUsage()
        self.model: str = "fake-model-v1"
        self.id: str = "chatcmpl-fake-role-001"


# ── spy gateway (records every chat() call; never calls litellm) ───────────────────────


class _SpyGateway:
    """Records every chat() invocation; returns a canned response; never calls litellm."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.calls.clear()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        mode: Any = None,
        caller_tag: str = "gateway",
    ) -> _FakeModelResponse:
        self.calls.append({"messages": messages, "mode": mode, "caller_tag": caller_tag})
        return _FakeModelResponse()


# ── claims-parameterized fake verifier ──────────────────────────────────────────────────
# Accepts exactly one fixed token ("good"); the claims it returns are driven by a mutable
# slot the test sets immediately before each request. Each hypothesis example drives ONE
# synchronous HTTP request via TestClient (no concurrency), so mutating `.next_claims`
# right before the POST is deterministic and race-free.


class _ClaimsVerifier:
    """Accepts token 'good'; returns whatever claims the test staged in `.next_claims`."""

    def __init__(self) -> None:
        self.next_claims: Mapping[str, Any] = {"sub": "unset"}

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return self.next_claims


# ── module-level app + spy + verifier (built once; state reset/staged per example) ─────

_SPY = _SpyGateway()
_VERIFIER = _ClaimsVerifier()

# CONTRACT: create_app must accept llm_gateway= (already true post-S3a) and verifier=.
_APP = create_app(
    settings=Settings(environment="test"),
    verifier=_VERIFIER,
    llm_gateway=_SPY,  # type: ignore[call-arg]
)
_CLIENT = TestClient(_APP, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer good"}


def _claims_with_llm_role(subject: str) -> dict[str, Any]:
    """Claims for a principal holding exactly {WM_LLM_ROLE} (plus `sub`)."""
    return {"sub": subject, ZITADEL_PROJECT_ROLES_CLAIM: {WM_LLM_ROLE: {}}}


# ── Hypothesis strategies (mirrors _openai_body from test_api_llm_gateway_delegation.py) ──

_MSG_ROLE = st.sampled_from(["user", "assistant", "system"])

_CONTENT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=200,
)

_MESSAGE = st.fixed_dictionaries({"role": _MSG_ROLE, "content": _CONTENT})
_MESSAGES = st.lists(_MESSAGE, min_size=1, max_size=5)

_MODEL_STR = st.one_of(
    st.sampled_from(["gpt-4o", "gpt-3.5-turbo", "claude-3-sonnet-20240229"]),
    st.text(
        alphabet=st.characters(min_codepoint=65, max_codepoint=122, categories=("Lu", "Ll")),
        min_size=1,
        max_size=20,
    ),
)


@st.composite
def _openai_body(draw: st.DrawFn) -> dict[str, Any]:
    """A valid OpenAI-shaped request body with varied messages + model."""
    body: dict[str, Any] = {
        "messages": draw(_MESSAGES),
        "model": draw(_MODEL_STR),
    }
    if draw(st.booleans()):
        body["temperature"] = draw(st.floats(min_value=0.0, max_value=2.0, allow_nan=False))
    return body


_MINIMAL_BODY: dict[str, Any] = {
    "messages": [{"role": "user", "content": "x"}],
    "model": "gpt-4o",
}

# Any string, INCLUDING empty — the empty case is the "hermes" fallback.
_SUBJECT = st.text(max_size=40)

_DECOY_ROLES = st.sampled_from(["worldmonitor:graph-read", "worldmonitor:admin", "some:other-role"])


@st.composite
def _no_wm_llm_role_claims(draw: st.DrawFn) -> dict[str, Any]:
    """Claims WITHOUT worldmonitor:llm: absent claim key, empty mapping, or decoys-only."""
    shape = draw(st.sampled_from(["absent", "empty", "decoys"]))
    claims: dict[str, Any] = {"sub": "role-gate-no-role-subject"}
    if shape == "empty":
        claims[ZITADEL_PROJECT_ROLES_CLAIM] = {}
    elif shape == "decoys":
        decoys = draw(st.sets(_DECOY_ROLES, min_size=1, max_size=3))
        claims[ZITADEL_PROJECT_ROLES_CLAIM] = {d: {} for d in decoys}
    # "absent": the claim key is simply not present in `claims` at all.
    return claims


@st.composite
def _with_wm_llm_role_claims(draw: st.DrawFn) -> dict[str, Any]:
    """Claims WITH worldmonitor:llm, optionally alongside 0-2 decoy roles."""
    decoys = draw(st.sets(_DECOY_ROLES, max_size=2))
    roles: dict[str, Any] = dict.fromkeys(decoys, {})
    roles[WM_LLM_ROLE] = {}
    return {"sub": "role-gate-with-role-subject", ZITADEL_PROJECT_ROLES_CLAIM: roles}


# (has_role: bool, claims: dict) pairs covering both branches of the gate.
_ROLE_CASE = st.one_of(
    _no_wm_llm_role_claims().map(lambda c: (False, c)),
    _with_wm_llm_role_claims().map(lambda c: (True, c)),
)

_PROP_SETTINGS = hyp_settings(
    max_examples=100,
    deadline=None,  # per repo convention: app-build/TestClient can be slow on busy runners
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ── P-CALLER: caller_tag == authenticated subject, "hermes" fallback only on empty ─────


@given(subject=_SUBJECT, body=_openai_body())
@example(subject="", body=_MINIMAL_BODY)  # guarantee the fallback branch is always exercised
@_PROP_SETTINGS
def test_caller_tag_is_authenticated_subject_with_hermes_fallback(
    subject: str, body: dict[str, Any]
) -> None:
    """P-CALLER (INV-CALLER): the gateway receives caller_tag == principal.subject.

    Over generated subjects (including the empty string) with a principal that HOLDS
    worldmonitor:llm (role held so the request is authorized — this test isolates
    attribution, not the role gate; that is P-ROLE below):
    - response is 200
    - the spy is called exactly once
    - spy.calls[0]["caller_tag"] == subject, EXCEPT when subject == "" it must be "hermes"

    NON-VACUITY: today's hardcoded caller_tag="hermes" (api/llm.py) fails this assertion
    for any generated subject that is non-empty and != "hermes" — which dominates the
    generated space (only the single string "hermes" itself would coincidentally pass).
    """
    _SPY.reset()
    _VERIFIER.next_claims = _claims_with_llm_role(subject)
    resp = _CLIENT.post("/v1/chat/completions", json=body, headers=_auth())
    assert resp.status_code == 200, (
        f"authenticated request holding worldmonitor:llm must yield 200, "
        f"got {resp.status_code}: {resp.text!r}"
    )
    assert len(_SPY.calls) == 1, (
        f"expected exactly 1 gateway.chat() call, got {len(_SPY.calls)}: {_SPY.calls!r}"
    )
    expected = subject or "hermes"
    assert _SPY.calls[0]["caller_tag"] == expected, (
        f"caller_tag must be the authenticated subject ({subject!r}), falling back to "
        f"'hermes' only when it is empty; expected {expected!r}, "
        f"got {_SPY.calls[0]['caller_tag']!r}"
    )


# ── P-ROLE: no worldmonitor:llm -> 403 + gateway never called; with it -> 200 + 1 call ──


@given(case=_ROLE_CASE, body=_openai_body())
@_PROP_SETTINGS
def test_role_gate_403_without_role_200_with_role_gateway_call_count(
    case: tuple[bool, dict[str, Any]], body: dict[str, Any]
) -> None:
    """P-ROLE (INV-ROLE): a valid token without worldmonitor:llm -> 403, gateway untouched;
    with it -> 200, gateway called exactly once.

    Over a drawn role-set that either omits worldmonitor:llm (possibly with decoy roles,
    an empty mapping, or the claim key entirely absent) or includes it (possibly alongside
    decoys):
    - no-role case: 403, spy.calls stays empty (no wasted egress on a forbidden request)
    - with-role case: 200, spy.calls has exactly one entry

    NON-VACUITY: an always-403 implementation fails the with-role branch; today's
    always-allow behaviour (Depends(get_principal), no role check) fails the no-role
    branch (200 instead of 403, and the gateway IS called).
    """
    has_role, claims = case
    _SPY.reset()
    _VERIFIER.next_claims = claims
    resp = _CLIENT.post("/v1/chat/completions", json=body, headers=_auth())
    if has_role:
        assert resp.status_code == 200, (
            f"a valid token holding worldmonitor:llm must yield 200, "
            f"got {resp.status_code}: {resp.text!r}; claims={claims!r}"
        )
        assert len(_SPY.calls) == 1, (
            f"expected exactly 1 gateway.chat() call for an authorized request, "
            f"got {len(_SPY.calls)}: {_SPY.calls!r}"
        )
    else:
        assert resp.status_code == 403, (
            f"a valid token WITHOUT worldmonitor:llm must yield 403, "
            f"got {resp.status_code}: {resp.text!r}; claims={claims!r}"
        )
        assert len(_SPY.calls) == 0, (
            f"gateway must NEVER be called when the role is missing (no wasted egress), "
            f"got {len(_SPY.calls)} calls: {_SPY.calls!r}"
        )


# ── P-ROLE (tokenless clause): unauthenticated != forbidden ────────────────────────────


@given(body=_openai_body())
@_PROP_SETTINGS
def test_role_gate_tokenless_request_returns_401_not_403(body: dict[str, Any]) -> None:
    """P-ROLE tokenless clause: a request with NO Authorization header -> 401, not 403.

    Proves require_llm_role composes on top of get_principal's existing 401 (unauthenticated)
    rather than swallowing it into a blanket 403 (forbidden) — unauthenticated and forbidden
    are distinct outcomes, and get_principal itself is unchanged (FROZEN).
    """
    _SPY.reset()
    resp = _CLIENT.post("/v1/chat/completions", json=body)  # no Authorization header at all
    assert resp.status_code == 401, (
        f"a tokenless request must yield 401 (unauthenticated), not 403 (forbidden), "
        f"got {resp.status_code}: {resp.text!r}"
    )
    assert len(_SPY.calls) == 0, (
        f"gateway must NEVER be called for an unauthenticated request, "
        f"got {len(_SPY.calls)} calls: {_SPY.calls!r}"
    )
