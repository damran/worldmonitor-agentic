"""Gate 3d — browser session auth: Zitadel OIDC login + dual-path AuthMiddleware (ADR 0068).

These are the PRIMARY invariant (oracle) tests for the platform's *browser* auth boundary. They
run entirely over an INJECTED OAuth client + an injected fake bearer verifier — there is NO live
Zitadel and NO network. RED on the base tree because: ``api/auth_web.py`` does not exist, the
``/login`` / ``/auth/callback`` / ``/logout`` routes are missing, ``create_app`` has no ``oauth=``
parameter, ``AuthMiddleware`` is bearer-only (no session path, no html-redirect), and ``Settings``
has no ``session_secret_key`` / ``app_base_url`` / ``zitadel_client_secret`` fields.

LOCKED ASSUMPTIONS — the builder MUST match these exactly so this oracle stays meaningful:

1. APP FACTORY DI. ``create_app(*, settings, verifier, readiness, neo4j_client, oauth=None)``
   gains a keyword-only ``oauth`` (an Authlib-style registry exposing ``.zitadel`` with async
   ``authorize_redirect(request, redirect_uri)`` and ``authorize_access_token(request)``). When
   injected it is used verbatim (no real Zitadel/discovery) and stored at ``app.state.oauth``.

2. MIDDLEWARE ORDER. ``create_app`` adds ``SessionMiddleware`` AFTER ``AuthMiddleware`` so the
   session middleware is OUTERMOST (Starlette: last-added == outermost) and ``request.session`` is
   populated before ``AuthMiddleware`` runs. Cookie: ``secret_key = settings.session_secret_key``,
   ``same_site="lax"``, httponly (Starlette default), ``https_only`` only outside dev (absent in
   ``environment="test"``). The session cookie name is the Starlette default ``"session"``.

3. AUTH WEB ROUTES (PUBLIC, in ``api/auth_web.py``):
     - ``GET /login?next=<local-path>``: validate ``next`` to a local path, stash it in the
       session, then
       ``return await request.app.state.oauth.zitadel.authorize_redirect(request, redirect_uri)``
       where ``redirect_uri = settings.app_base_url.rstrip("/") + "/auth/callback"``.
     - ``GET /auth/callback``:
       ``token = await request.app.state.oauth.zitadel.authorize_access_token(request)``;
       read ``userinfo = token["userinfo"]`` (a mapping carrying ``sub``); set
       ``request.session["principal"] = {"subject": userinfo["sub"], "claims": dict(userinfo)}``;
       302 to the (validated) stashed ``next`` (default ``"/"``). A state mismatch (Authlib raises
       ``MismatchingStateError``) must SURFACE AN ERROR (>= 400), never store a principal, never
       bounce to ``/login``.
     - ``GET /logout``: ``request.session.clear()`` then 302 to ``"/"``.

4. DUAL-PATH ``AuthMiddleware._authenticate``: public -> allow; BEARER PRESENT -> verify via the
   existing ``TokenVerifier`` (valid -> principal; invalid/missing-verifier -> 401, byte-for-byte
   the old behaviour, EVEN for an ``Accept: text/html`` request); else SESSION principal present
   -> rebuild ``Principal.from_claims(request.session["principal"]["claims"])`` -> allow; else
   unauthenticated -> if ``Accept`` contains ``text/html`` -> 302 ``/login?next=<request-path>``
   else 401. (Because ``claims`` carries ``sub``, the rebuilt principal's ``subject`` is the IdP
   ``sub``.)

5. NO OPEN REDIRECT. ``next`` is honoured only if it is a LOCAL path: starts with a single ``/``,
   not ``//``, no scheme (``http:``/``https:``/``javascript:``), no backslash. Anything else falls
   back to ``"/"``. The final callback redirect ``Location`` is ALWAYS a local path, never the
   attacker target.

6. SETTINGS / FAIL-CLOSED. New fields ``session_secret_key``, ``zitadel_client_secret``,
   ``app_base_url``. ``validate_production_secrets()`` rejects an empty/placeholder
   ``session_secret_key`` (and ``zitadel_client_secret``) WHEN ``auth_configured`` and outside
   {development, test}. (That fail-closed case lives in ``test_production_secret_hygiene.py``.)
"""

from __future__ import annotations

import json
import secrets
from base64 import b64encode
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import itsdangerous
import pytest
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.starlette_client import OAuth
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

from worldmonitor.api.auth_web import _is_safe_next, build_oauth
from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.settings import Settings

# --- Locked test constants (mirror the builder's settings field names) --------------------------
SESSION_SECRET = "test-session-key-0123456789"
ZITADEL_DOMAIN = "auth.example.test"
ZITADEL_CLIENT_ID = "wm-client"
ZITADEL_CLIENT_SECRET = "test-client-secret-abcdef"
APP_BASE_URL = "https://wm.example.com"
EXPECTED_REDIRECT_URI = "https://wm.example.com/auth/callback"
EXPECTED_AUTHORIZE_URL = f"https://{ZITADEL_DOMAIN}/oauth/v2/authorize"

# The canned identity the (fake) IdP returns at the callback. ``sub`` rides in ``claims`` so the
# middleware's ``Principal.from_claims`` reconstructs subject == "user-7".
SESSION_USERINFO: dict[str, Any] = {
    "sub": "user-7",
    "email": "user7@example.test",
    "name": "User Seven",
}


# ================================================================================================
# Fakes — a bearer verifier (mirrors test_api_health), a placeholder graph client, and an injected
# OAuth registry standing in for Zitadel (no network).
# ================================================================================================
class _FakeVerifier:
    """Accepts the bearer token ``"good"``; rejects everything else (mirrors test_api_health)."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


class _FakeNeo4j:
    """Placeholder read client stored on ``app.state``; the auth routes / ``/me`` never touch it."""

    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("the graph client must not be used by the session-auth tests")


class _FakeZitadelClient:
    """A controllable stand-in for ``oauth.zitadel`` — no network, real session round-trip.

    ``authorize_redirect`` mimics Authlib: it writes a random ``state`` into ``request.session`` and
    302s to a Zitadel-shaped authorize URL carrying that state. ``authorize_access_token`` validates
    the returned ``state`` against the session (raising Authlib's ``MismatchingStateError`` on a
    mismatch — exactly what real Authlib raises) and otherwise returns a canned token whose
    ``"userinfo"`` carries ``sub``.
    """

    _STATE_KEY = "_fake_oauth_state"

    def __init__(self, userinfo: Mapping[str, Any] | None = None) -> None:
        self._userinfo: dict[str, Any] = dict(userinfo or SESSION_USERINFO)

    async def authorize_redirect(
        self, request: Any, redirect_uri: str | None = None, **kwargs: Any
    ) -> RedirectResponse:
        state = secrets.token_urlsafe(16)
        request.session[self._STATE_KEY] = state
        params = {
            "response_type": "code",
            "client_id": ZITADEL_CLIENT_ID,
            "redirect_uri": redirect_uri or EXPECTED_REDIRECT_URI,
            "scope": "openid profile email",
            "state": state,
            "code_challenge": "fake-pkce-challenge",
            "code_challenge_method": "S256",
        }
        return RedirectResponse(f"{EXPECTED_AUTHORIZE_URL}?{urlencode(params)}", status_code=302)

    async def authorize_access_token(self, request: Any, **kwargs: Any) -> dict[str, Any]:
        sent = request.query_params.get("state")
        stored = request.session.pop(self._STATE_KEY, None)
        if not sent or sent != stored:
            raise MismatchingStateError()
        return {"access_token": "fake-access-token", "userinfo": dict(self._userinfo)}


class _FakeOAuth:
    """Injected OAuth registry: exposes ``.zitadel`` like an Authlib registry."""

    def __init__(self, userinfo: Mapping[str, Any] | None = None) -> None:
        self.zitadel = _FakeZitadelClient(userinfo)


def _real_oauth() -> OAuth:
    """A REAL Authlib registry with EXPLICIT endpoints so ``authorize_redirect`` builds the URL
    OFFLINE (no discovery, no network). Used only to assert the login URL is FAITHFUL (state +
    PKCE), never a tautology over a hand-rolled fake."""
    oauth = OAuth()
    oauth.register(
        name="zitadel",
        client_id=ZITADEL_CLIENT_ID,
        client_secret=ZITADEL_CLIENT_SECRET,
        authorize_url=EXPECTED_AUTHORIZE_URL,
        access_token_url=f"https://{ZITADEL_DOMAIN}/oauth/v2/token",
        jwks_uri=f"https://{ZITADEL_DOMAIN}/oauth/v2/keys",
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    return oauth


# ================================================================================================
# App / client builders
# ================================================================================================
def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "session_secret_key": SESSION_SECRET,
        "zitadel_domain": ZITADEL_DOMAIN,
        "zitadel_client_id": ZITADEL_CLIENT_ID,
        "zitadel_client_secret": ZITADEL_CLIENT_SECRET,
        "app_base_url": APP_BASE_URL,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _make_client(
    *,
    verifier: object | None = None,
    oauth: object | None = None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    app = create_app(
        settings=_settings(),
        verifier=verifier if verifier is not None else _FakeVerifier(),  # type: ignore[arg-type]
        oauth=oauth if oauth is not None else _FakeOAuth(),  # type: ignore[call-arg]
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _drive_login_and_callback(
    client: TestClient, *, next_param: str | None = None
) -> tuple[Any, Any]:
    """Drive ``/login`` -> ``/auth/callback`` with the fake OAuth, returning both responses. The
    TestClient jar carries the signed session cookie across the two requests (a REAL round-trip
    through the real SessionMiddleware)."""
    params = {} if next_param is None else {"next": next_param}
    r_login = client.get("/login", params=params, follow_redirects=False)
    assert r_login.status_code == 302, f"/login must 302 to the IdP, got {r_login.status_code}"
    state = parse_qs(urlparse(r_login.headers["location"]).query).get("state", [None])[0]
    assert state, "the login redirect must carry an OAuth state param"
    r_cb = client.get(
        "/auth/callback",
        params={"code": "fake-code", "state": state},
        follow_redirects=False,
    )
    return r_login, r_cb


def _tamper(cookie: str) -> str:
    """Corrupt the signed cookie's payload so its HMAC no longer verifies (SessionMiddleware then
    silently drops the session)."""
    parts = cookie.split(".")
    payload = parts[0]
    flipped = ("Z" if payload[:1] != "Z" else "Y") + payload[1:]
    return ".".join([flipped] + parts[1:])


# ================================================================================================
# A. DUAL-PATH MIDDLEWARE — the security core.
# ================================================================================================
def test_valid_bearer_authenticates() -> None:
    """Bearer path UNCHANGED: a valid bearer -> 200 with the bearer principal."""
    resp = _make_client().get("/me", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    assert resp.json() == {"subject": "user-123"}


def test_invalid_bearer_401() -> None:
    """Bearer path UNCHANGED: an invalid bearer -> 401 (never a session/login fallthrough)."""
    resp = _make_client().get(
        "/me", headers={"Authorization": "Bearer wrong"}, follow_redirects=False
    )
    assert resp.status_code == 401


def test_invalid_bearer_text_html_still_401() -> None:
    """A PRESENT-but-invalid bearer 401s even for a browser (Accept: text/html) — a bad token must
    NOT bounce to /login (that would mask a failed token behind a login screen)."""
    resp = _make_client().get(
        "/me",
        headers={"Authorization": "Bearer wrong", "Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 401, "an invalid bearer must 401 even when the caller accepts html"


@pytest.mark.parametrize("accept", ["application/json", "*/*"])
def test_no_auth_json_request_401(accept: str) -> None:
    """No token + a JSON/``*/*`` Accept -> 401, NOT a redirect. This preserves the API contract the
    frozen api-auth tests rely on (a tokenless API request must 401, never 302)."""
    resp = _make_client().get("/me", headers={"Accept": accept}, follow_redirects=False)
    assert resp.status_code == 401, f"Accept={accept!r} must 401, got {resp.status_code}"


def test_no_auth_html_request_redirects_to_login() -> None:
    """No token + Accept: text/html (a browser) -> 302 to /login?next=<path>."""
    resp = _make_client().get("/me", headers={"Accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 302
    loc = urlparse(resp.headers["location"])
    assert loc.path == "/login", (
        f"browser must be redirected to /login, got {resp.headers['location']!r}"
    )
    nxt = parse_qs(loc.query).get("next", [None])[0]
    assert nxt == "/me", f"the login redirect must carry next=/me, got {nxt!r}"


def test_valid_session_authenticates() -> None:
    """SESSION path: after the OIDC round-trip sets a signed session principal, a tokenless request
    carrying the session cookie authenticates as the IdP subject."""
    client = _make_client()
    _, r_cb = _drive_login_and_callback(client)
    assert r_cb.status_code == 302, f"callback must 302 after storing the principal: {r_cb.text}"
    resp = client.get("/me")  # no bearer; the signed session cookie rides the jar
    assert resp.status_code == 200, (
        f"valid session must authenticate: {resp.status_code} {resp.text}"
    )
    assert resp.json() == {"subject": "user-7"}


def test_tampered_session_cookie_does_not_authenticate() -> None:
    """A tampered session cookie fails the HMAC -> SessionMiddleware drops it -> 401 (a forged
    session cannot impersonate a principal)."""
    client = _make_client()
    _drive_login_and_callback(client)
    cookie = client.cookies.get("session")
    assert cookie, "the callback must set a signed 'session' cookie"
    tampered = _tamper(cookie)
    fresh = _make_client()  # same session secret, FRESH jar (no valid cookie to override)
    resp = fresh.get(
        "/me",
        headers={"Cookie": f"session={tampered}", "Accept": "application/json"},
        follow_redirects=False,
    )
    assert resp.status_code == 401, "a tampered session cookie must NOT authenticate"


# ================================================================================================
# B. OIDC ROUTES.
# ================================================================================================
def test_login_redirects_to_zitadel_authorize_with_state_and_pkce() -> None:
    """/login -> 302 to the Zitadel authorize endpoint carrying response_type=code + state + a PKCE
    S256 code_challenge + client_id + the redirect_uri built from app_base_url. Asserted against a
    REAL Authlib registry (offline) so the URL is FAITHFUL, not a hand-rolled tautology."""
    resp = _make_client(oauth=_real_oauth()).get("/login", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith(EXPECTED_AUTHORIZE_URL), f"login must 302 to the IdP authorize URL: {loc}"
    q = parse_qs(urlparse(loc).query)
    assert q.get("response_type") == ["code"], q
    assert q.get("code_challenge_method") == ["S256"], q
    assert q.get("code_challenge", [""])[0], "PKCE code_challenge must be present"
    assert q.get("state", [""])[0], "CSRF state must be present"
    assert q.get("client_id") == [ZITADEL_CLIENT_ID], q
    assert q.get("redirect_uri") == [EXPECTED_REDIRECT_URI], (
        f"redirect_uri must be the absolute app_base_url callback {EXPECTED_REDIRECT_URI!r} "
        f"(NOT a Host-derived URL), got {q.get('redirect_uri')!r}"
    )


def test_callback_stores_principal_and_redirects_next() -> None:
    """/auth/callback (fake token with userinfo sub=user-7) stores the principal and 302s to the
    validated stashed next; a later request is authenticated as user-7."""
    client = _make_client()
    _, r_cb = _drive_login_and_callback(client, next_param="/dashboard")
    assert r_cb.status_code == 302
    assert urlparse(r_cb.headers["location"]).path == "/dashboard", (
        f"callback must honour the validated next, got {r_cb.headers['location']!r}"
    )
    me = client.get("/me")
    assert me.status_code == 200
    assert me.json() == {"subject": "user-7"}


def test_callback_rejects_bad_state() -> None:
    """A callback whose state does not match the session is rejected (Authlib
    MismatchingStateError): the route SURFACES AN ERROR (>= 400), stores no principal, and does
    not bounce to /login."""
    client = _make_client(raise_server_exceptions=False)
    r_login = client.get("/login", follow_redirects=False)  # establishes a real session state
    assert r_login.status_code == 302
    resp = client.get(
        "/auth/callback",
        params={"code": "x", "state": "not-the-real-state"},
        follow_redirects=False,
    )
    assert resp.status_code >= 400, (
        f"a state mismatch must be an ERROR, not a success/login redirect: {resp.status_code}"
    )
    me = client.get("/me", headers={"Accept": "application/json"}, follow_redirects=False)
    assert me.status_code == 401, "no principal may be stored on a state mismatch"


def test_callback_rejects_bad_state_real_authlib() -> None:
    """Same CSRF guard, proven against REAL Authlib: a callback with no matching session state
    raises MismatchingStateError BEFORE any network -> the route surfaces an error, stores no
    principal."""
    client = _make_client(oauth=_real_oauth(), raise_server_exceptions=False)
    resp = client.get(
        "/auth/callback",
        params={"code": "x", "state": "anything"},
        follow_redirects=False,
    )
    assert resp.status_code >= 400, f"real-Authlib state mismatch must error: {resp.status_code}"
    me = client.get("/me", headers={"Accept": "application/json"}, follow_redirects=False)
    assert me.status_code == 401


def test_logout_clears_session() -> None:
    """/logout clears the session and 302s to /; a following tokenless JSON request -> 401."""
    client = _make_client()
    _drive_login_and_callback(client)
    assert client.get("/me").status_code == 200, "precondition: logged in"
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert urlparse(resp.headers["location"]).path == "/"
    me = client.get("/me", headers={"Accept": "application/json"}, follow_redirects=False)
    assert me.status_code == 401, "the session must be cleared after /logout"


# ================================================================================================
# C. NO OPEN REDIRECT — our code, high value.
# ================================================================================================
HOSTILE_NEXT = [
    "//evil.com",
    "https://evil.com",
    "http://evil.com",
    "\\evil.com",
    "/\\evil.com",
    "javascript:alert(1)",
]


@pytest.mark.parametrize("hostile", HOSTILE_NEXT)
def test_next_open_redirect_refused(hostile: str) -> None:
    """A hostile ``next`` is never honoured: after the full login->callback round-trip the redirect
    Location is a LOCAL path (no scheme, no netloc, not protocol-relative), never the attacker
    target."""
    client = _make_client()
    _, r_cb = _drive_login_and_callback(client, next_param=hostile)
    assert r_cb.status_code == 302, f"callback should still redirect locally: {r_cb.status_code}"
    loc = r_cb.headers["location"]
    parsed = urlparse(loc)
    assert parsed.scheme == "", f"open redirect via scheme: {loc!r}"
    assert parsed.netloc == "", f"open redirect via netloc: {loc!r}"
    assert not loc.startswith("//"), f"protocol-relative open redirect: {loc!r}"
    assert loc.startswith("/"), f"redirect must be a local path: {loc!r}"
    assert "evil.com" not in loc, f"attacker host leaked into the redirect: {loc!r}"
    assert not loc.lower().startswith("javascript:"), f"javascript: scheme honoured: {loc!r}"


# ================================================================================================
# D. SESSION COOKIE FLAGS + middleware ordering.
# ================================================================================================
def test_session_cookie_is_httponly_and_samesite_lax() -> None:
    """The session cookie set at the callback is HttpOnly + SameSite=Lax (secure depends on env and
    is not asserted in test)."""
    client = _make_client()
    _, r_cb = _drive_login_and_callback(client)
    set_cookies = r_cb.headers.get_list("set-cookie")
    session_cookies = [c for c in set_cookies if c.lower().startswith("session=")]
    assert session_cookies, f"the callback must (re)set the session cookie: {set_cookies!r}"
    flags = session_cookies[0].lower()
    assert "httponly" in flags, f"session cookie must be HttpOnly: {session_cookies[0]!r}"
    assert "samesite=lax" in flags, f"session cookie must be SameSite=Lax: {session_cookies[0]!r}"


def test_session_middleware_is_outer_of_auth() -> None:
    """SessionMiddleware must be OUTER of AuthMiddleware (added AFTER it -> lower user_middleware
    index) so request.session is populated before AuthMiddleware reads it."""
    app = create_app(
        settings=_settings(),
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        oauth=_FakeOAuth(),  # type: ignore[call-arg]
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
    )
    names = [mw.cls.__name__ for mw in app.user_middleware]
    assert "SessionMiddleware" in names, f"SessionMiddleware must be installed: {names}"
    assert "AuthMiddleware" in names, f"AuthMiddleware must be installed: {names}"
    assert names.index("SessionMiddleware") < names.index("AuthMiddleware"), (
        f"SessionMiddleware must be OUTER of AuthMiddleware (lower index): {names}"
    )


# ================================================================================================
# E. ADVERSARIAL AUTH-BOUNDARY REGRESSIONS (RED on the current tree).
#
# These pin three auth-boundary bugs an adversarial review found that the green fixtures masked:
#   * the SESSION path authenticates even with NO verifier / auth unconfigured (must fail closed);
#   * the dev SessionMiddleware fallback key is a PUBLISHED shared constant (a cookie minted under
#     one process cross-validates under another — it must be a per-process random key);
#   * the PRODUCTION ``build_oauth`` omits PKCE ``code_challenge_method=S256`` (the fake/_real_oauth
#     fixtures hand-add it, masking the gap);
#   * ``_is_safe_next`` honours next values carrying ASCII control chars / non-clean local paths.
# ================================================================================================


def _unconfigured_settings(**overrides: Any) -> Settings:
    """OIDC auth NOT configured, independent of any local ``.env`` (``_env_file=None``).

    ``auth_configured`` stays False unless an override sets domain + client id.
    """
    base: dict[str, Any] = {
        "_env_file": None,
        "zitadel_domain": "",
        "zitadel_client_id": "",
        "zitadel_client_secret": "",
        "app_base_url": "",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _mint_session_cookie(secret_key: str, session: dict[str, Any]) -> str:
    """Forge a *validly signed* Starlette session cookie the way ``SessionMiddleware`` does:
    ``itsdangerous.TimestampSigner(secret_key).sign(b64encode(json.dumps(session)))`` (see
    ``starlette/middleware/sessions.py``). The HMAC verifies, so ``SessionMiddleware`` populates
    ``request.session`` from it — a 401 is then the AUTH boundary refusing, not a bad cookie.
    """
    signer = itsdangerous.TimestampSigner(secret_key)
    data = b64encode(json.dumps(session).encode("utf-8"))
    return signer.sign(data).decode("utf-8")


def test_session_path_fails_closed_when_verifier_is_none() -> None:
    """FINDING 1 (session path): with NO verifier / auth unconfigured, a *validly signed* session
    cookie must NOT authenticate — the session path must FAIL CLOSED, mirroring the bearer path's
    'Authentication is not configured' 401.

    RED now: ``AuthMiddleware._session_principal`` ignores ``self._verifier`` entirely, so a signed
    cookie authenticates a forged principal even when no auth is configured (returns 200).
    """
    app = create_app(
        settings=_unconfigured_settings(environment="test", session_secret_key="test-key-abc-123"),
        verifier=None,
        oauth=_FakeOAuth(),  # type: ignore[arg-type]
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
    )
    cookie = _mint_session_cookie(
        "test-key-abc-123",
        {"principal": {"subject": "attacker", "claims": {"sub": "attacker"}}},
    )
    resp = TestClient(app).get(
        "/me",
        headers={"Cookie": f"session={cookie}", "Accept": "application/json"},
        follow_redirects=False,
    )
    assert resp.status_code == 401, (
        "session path must FAIL CLOSED with no verifier/auth configured (mirror the bearer "
        f"path's 'Authentication is not configured' 401); got {resp.status_code} {resp.text!r}"
    )


def test_dev_fallback_session_key_is_not_a_shared_published_constant() -> None:
    """FINDING 1 (published fallback key): a session cookie minted by one dev process must NOT
    cross-validate against another — each must use a DISTINCT per-process RANDOM fallback signing
    key, never a hardcoded published constant.

    RED now: both apps fall back to the same hardcoded ``_DEV_SESSION_FALLBACK_KEY``, so app1's
    cookie is accepted by app2 (returns 200 == anyone holding the published key forges any session).
    """

    def _dev_app(sub: str) -> Any:
        return create_app(
            settings=_unconfigured_settings(environment="development", session_secret_key=""),
            verifier=_FakeVerifier(),  # non-None: isolates this from the verifier-None finding
            oauth=_FakeOAuth({"sub": sub}),  # type: ignore[arg-type]
            neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
        )

    app1 = _dev_app("u1")
    app2 = _dev_app("u1")

    c1 = TestClient(app1)
    _, r_cb = _drive_login_and_callback(c1)
    assert r_cb.status_code == 302, (
        f"app1 login/callback must succeed: {r_cb.status_code} {r_cb.text}"
    )
    cookie = c1.cookies.get("session")
    assert cookie, "app1's callback must set a signed 'session' cookie"
    # Sanity: the cookie is a REAL, valid session against its OWN process (so a later 401 on app2 is
    # specifically the key-distinctness boundary, never a malformed/expired cookie).
    assert c1.get("/me").status_code == 200, "precondition: app1's own cookie authenticates"

    resp = TestClient(app2).get(
        "/me",
        headers={"Cookie": f"session={cookie}", "Accept": "application/json"},
        follow_redirects=False,
    )
    assert resp.status_code == 401, (
        "a session cookie minted by one dev process must NOT cross-validate against another: each "
        "process must use a per-process RANDOM fallback key, not a published shared constant; got "
        f"{resp.status_code} {resp.text!r}"
    )


def test_build_oauth_registers_pkce_s256() -> None:
    """FINDING 2 (production PKCE): the PRODUCTION ``build_oauth`` must register the Zitadel client
    for PKCE — Authlib reads ``code_challenge_method`` from the client's ``client_kwargs``.

    Exercises the real ``build_oauth`` (NOT the test-only ``_real_oauth`` fixture, which hand-adds
    S256 and masks the gap). RED now: ``build_oauth`` sets only ``client_kwargs={'scope': ...}``.
    """
    oauth = build_oauth(
        _unconfigured_settings(
            zitadel_domain="auth.example.com",
            zitadel_client_id="cid",
            zitadel_client_secret="csecret-value",
            app_base_url="https://wm.example.com",
        )
    )
    registered = oauth.zitadel  # the StarletteOAuth2App registered under name="zitadel"
    method = registered.client_kwargs.get("code_challenge_method")
    assert method == "S256", (
        "production build_oauth must register the confidential client for PKCE S256 (Authlib reads "
        f"code_challenge_method from client_kwargs); got client_kwargs={registered.client_kwargs!r}"
    )


# Actual control characters (NOT the literal backslash-n) embedded in an otherwise local-looking
# path. ``_is_safe_next`` currently honours each because it starts with "/", its 2nd char != "/",
# and it has no backslash — yet each carries a CR/LF/TAB/NUL/DEL or a raw space, so it is unsafe to
# render into an HTML href. id labels keep pytest output readable (control chars are escaped).
_CONTROL_CHAR_NEXT = [
    ("lf", "/\n//evil.com"),
    ("cr", "/\r//evil.com"),
    ("tab", "/\t//evil.com"),
    ("crlf", "/\r\n//evil.com"),
    ("nul", "/\x00//evil.com"),
    ("vtab", "/\x0b//evil.com"),
    ("formfeed", "/\x0c//evil.com"),
    ("del", "/\x7f//evil.com"),
    ("space", "/x y"),
]


@pytest.mark.parametrize(
    "hostile_next",
    [v for _, v in _CONTROL_CHAR_NEXT],
    ids=[name for name, _ in _CONTROL_CHAR_NEXT],
)
def test_is_safe_next_rejects_control_chars(hostile_next: str) -> None:
    """FINDING 3 (open redirect via control chars): ``_is_safe_next`` must be SELF-SUFFICIENT and
    reject any next carrying an ASCII control char (0x00-0x1F / 0x7F) or that is not a clean local
    path, falling back to ``"/"`` — it must NOT lean on a downstream RedirectResponse to quote it
    (the UI slice renders ``next`` into an HTML href).

    RED now: the guard only checks ``startswith('/')``, not ``//``, and no backslash — so a CR/LF/
    TAB/NUL/DEL or a raw space sails through and is returned verbatim.
    """
    result = _is_safe_next(hostile_next)
    assert result == "/", (
        "a next carrying an ASCII control char (0x00-0x1F / 0x7F) or not a clean local path must "
        f"fall back to the site root; got {result!r} for input {hostile_next!r}"
    )
