"""Browser OIDC login routes — Zitadel authorization-code flow (ADR 0068).

A human clicking an HTML page cannot send a bearer header, so the app logs the
browser in itself via the OIDC authorization-code flow and carries the identity
in a signed session cookie. Authlib (``oauth.zitadel``) handles the
**state (CSRF)**, **PKCE**, **nonce** and **id-token validation** — we never
hand-roll the flow. The three routes here are PUBLIC (added to the middleware's
public paths by ``create_app``) so an unauthenticated browser can complete login.

``next`` is validated to a LOCAL path (``_is_safe_next``) before it is ever used
as a redirect target — no open redirect. The Zitadel discovery/JWKS/token fetch
hits the operator-configured issuer via Authlib's httpx client (trusted config,
may be internal) — NOT through ``guarded_stream``.
"""

# Authlib ships no type stubs; the OAuth registry's ``.zitadel`` client is dynamic.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse, Response

if TYPE_CHECKING:
    from worldmonitor.settings import Settings

router = APIRouter(tags=["auth"])


def _is_safe_next(nxt: str | None) -> str:
    """Return ``nxt`` iff it is a safe LOCAL path, else ``"/"`` (no open redirect, ADR 0068/0069).

    A safe ``next`` starts with exactly one ``/`` (a same-origin path), contains no backslash, and
    contains ONLY **printable ASCII excluding space** (every char ``0x20 < ord(ch) < 0x7F``). This
    refuses ``//evil.com`` (protocol-relative), ``https://evil.com`` / ``javascript:...`` (a scheme
    — does not start with ``/``), ``\\evil.com`` and ``/\\evil.com`` (backslash, which some browsers
    normalize to ``/``), any value carrying an ASCII control char (CR/LF/TAB/NUL/VTAB/FF), a raw
    space or DEL, AND — tightened in ADR 0069 — every NON-ASCII char: the C1 control ``\x85`` (NEL),
    the Unicode line/paragraph separators (U+2028 / U+2029), and any other ``>= 0x80`` codepoint.

    The printable-ASCII rule makes the guard SELF-SUFFICIENT: it must NOT lean on a downstream
    ``RedirectResponse`` to percent-quote the value, because the UI slice renders ``next`` straight
    into an HTML ``href`` — a raw CR/LF, space, or a Unicode separator there is a header-splitting /
    attribute-injection vector. A legitimate local path ("/integrations", "/me") is all printable
    ASCII and passes; anything else falls back to the site root.
    """
    if (
        nxt
        and nxt.startswith("/")
        and not nxt.startswith("//")
        and "\\" not in nxt
        and all(0x20 < ord(ch) < 0x7F for ch in nxt)
    ):
        return nxt
    return "/"


def build_oauth(settings: Settings) -> OAuth:
    """Register the Zitadel OIDC client from settings (production path; tests inject a fake).

    Authlib loads the endpoints from the discovery document, so the client knows the authorize /
    token / JWKS URLs without us hardcoding them. ``client_kwargs`` requests the OIDC scopes and
    EXPLICITLY enables PKCE (``code_challenge_method="S256"``) — Authlib 1.7.2 only drives PKCE when
    this is set on the registration, so omitting it silently ships a non-PKCE flow (ADR 0068).
    """
    oauth = OAuth()
    oauth.register(
        name="zitadel",
        client_id=settings.zitadel_client_id,
        client_secret=settings.zitadel_client_secret.get_secret_value(),
        server_metadata_url=settings.oidc_discovery_url,
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    return oauth


def _oauth(request: Request) -> Any:
    """Return the injected/registered OAuth registry, or 503 if auth is not configured."""
    oauth = getattr(request.app.state, "oauth", None)
    if oauth is None:
        raise HTTPException(status_code=503, detail="Auth is not configured")
    return oauth


@router.get("/login", include_in_schema=False)
async def login(request: Request, next: str | None = None) -> Response:
    """Stash the validated ``next`` and 302 to Zitadel's authorize endpoint (state + PKCE)."""
    settings: Settings = request.app.state.settings
    request.session["next"] = _is_safe_next(next)

    base = settings.app_base_url.rstrip("/")
    redirect_uri = f"{base}/auth/callback" if base else str(request.url_for("auth_callback"))

    return await _oauth(request).zitadel.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request) -> Response:
    """Exchange the code (Authlib validates state + id-token), store the principal, 302 to next.

    On an OAuth error (e.g. ``MismatchingStateError`` — a missing/forged CSRF state) we SURFACE a
    400, store NO principal, and never bounce to ``/login`` (which would be a redirect loop that
    masks the failure).
    """
    try:
        token = await _oauth(request).zitadel.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail="OIDC callback failed") from exc

    userinfo: dict[str, Any] = dict(token.get("userinfo") or {})
    request.session["principal"] = {
        "subject": str(userinfo.get("sub", "")),
        "claims": userinfo,
    }
    nxt = _is_safe_next(request.session.pop("next", "/"))
    return RedirectResponse(nxt, status_code=302)


@router.get("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    """Clear the session cookie and 302 to the site root (local logout only, ADR 0068)."""
    request.session.clear()
    return RedirectResponse("/", status_code=302)
