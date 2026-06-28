# 0068 — Browser session auth: Zitadel OIDC login + dual-path AuthMiddleware (Phase-2 Stage-3 slice 4a)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/3d-session-auth` (off `master`). The auth foundation for the Integrations UI (slice 4b).
- **Decided with user:** the Integrations UI uses **in-app login (Session + Zitadel OIDC)** — self-contained,
  production-safe, no reverse-proxy dependency.
- **human_fork:** false (the mechanism was chosen by the user; the library/flow details are reversible).

## Context

The API is **bearer-token only** (`AuthMiddleware` checks `Authorization: Bearer`; `ZitadelTokenVerifier`
validates against Zitadel JWKS). A human clicking an HTML page can't send a bearer header. To make the
Integrations UI (next slice) usable in a browser **with real auth from commit zero**, the app must log a
browser in itself via the **OIDC authorization-code flow** and carry the identity in a signed **session
cookie**, while still accepting bearer tokens for API/agent callers.

## Decision

### 1. Library — **Authlib** (don't hand-roll OAuth) + Starlette `SessionMiddleware`
Use `authlib.integrations.starlette_client.OAuth` for the OIDC client — it handles the **state (CSRF)**,
**PKCE**, **nonce**, and **id-token validation** that are easy to get wrong by hand. Register Zitadel via
its **discovery URL** (`https://{zitadel_domain}/.well-known/openid-configuration`) with `client_id` +
`client_secret`, `scope="openid profile email"`. Session state rides in Starlette's `SessionMiddleware`
(signed cookie). New deps: `authlib`, `itsdangerous` (SessionMiddleware needs it). The existing
`ZitadelTokenVerifier` (PyJWT) stays the **bearer** path unchanged.

### 2. New settings (secrets)
- `zitadel_client_secret: str = ""` (**secret** — confidential client).
- `session_secret_key: str = ""` (**secret** — signs the session cookie).
- `app_base_url: str = ""` (e.g. `https://wm.example.com`) — to build the absolute `redirect_uri`.
- Derived: `oidc_discovery_url` = `https://{zitadel_domain}/.well-known/openid-configuration`.
- **Fail-closed:** `validate_production_secrets()` (ADR 0061) is extended to reject an empty/placeholder
  `session_secret_key` (and `zitadel_client_secret` when auth is configured) outside dev/test.

### 3. `SessionMiddleware` ordering (subtle, load-bearing)
`SessionMiddleware` must run **before** `AuthMiddleware` so `request.session` is populated when
`AuthMiddleware` reads it. In Starlette the **last-added** middleware is **outermost** → `create_app` adds
`SessionMiddleware` **after** `AuthMiddleware`. Cookie flags: `https_only=True` outside dev, `same_site="lax"`
(so the OIDC redirect back from Zitadel carries the cookie), httponly (Starlette default).

### 4. Auth web routes (a new `api/auth_web.py` router; the login paths are PUBLIC)
- `GET /login?next=<local-path>` → `oauth.zitadel.authorize_redirect(request, redirect_uri)` (Authlib stores
  state+PKCE+nonce in the session, 302s to Zitadel). `next` is stashed in the session and **validated to be a
  local path** (must start with a single `/`, not `//` or a scheme) — **no open redirect**.
- `GET /auth/callback` → `token = await oauth.zitadel.authorize_access_token(request)` (Authlib validates
  state + exchanges the code + validates the id_token incl. nonce/issuer/audience) → store
  `request.session["principal"] = {"subject", "claims"}` → 302 to the validated `next` (default `/`).
- `GET /logout` → `request.session.clear()` → 302 to `/` (optionally Zitadel end-session later).
- `/login`, `/auth/callback`, `/logout` are added to the middleware's **public paths** (reachable
  unauthenticated). The `.well-known`/JWKS fetches go to the **operator-configured Zitadel issuer** (trusted),
  via Authlib's httpx client — NOT through `guarded_stream` (the SSRF guard blocks loopback/internal hosts, and
  Zitadel may legitimately be internal; the issuer is config, not attacker input).

### 5. Dual-path `AuthMiddleware` (the security-critical change)
`_authenticate(request)`:
1. public path → allow.
2. **Bearer present** → verify via the existing `TokenVerifier` (unchanged): valid → set principal; invalid /
   verifier-missing → **401** (API contract preserved exactly).
3. else **session principal present** (set at callback) → reconstruct `Principal` from `request.session`,
   set it → allow.
4. else unauthenticated → if the request `Accept` header contains `text/html` (a browser) → **302 redirect to
   `/login?next=<path>`**; otherwise → **401** (API/JSON callers, incl. the existing tests, unchanged).

The session can't be forged (signed by `session_secret_key`); an invalid bearer still 401s; the bearer path
is tried first and is byte-for-byte the old behaviour — so every existing API auth test stays green.

### 6. DI for testability
`create_app(*, oauth: OAuth | None = None, ...)` — inject a fake OAuth (canned `authorize_redirect` /
`authorize_access_token`) so tests drive the flow with **no live Zitadel and no network**. The real OAuth is
built from settings when `auth_configured` (else the login routes 404/redirect to a "auth not configured"
notice). Mirrors the existing `verifier=`/`neo4j_client=` injection.

## Alternatives considered
- **Hand-roll the OIDC flow (httpx + PyJWT).** Fewer deps, but re-implements state/PKCE/nonce/id-token
  validation — the exact security-critical bits Authlib already gets right. Rejected for an auth boundary.
- **Reverse proxy (oauth2-proxy).** Zero app code but needs proxy infra to click the UI; the user chose the
  self-contained in-app flow.
- **Public client (PKCE-only, no secret).** Viable, but a server-side confidential client (client_secret +
  PKCE) is the stronger posture for a server that can keep a secret. Chosen.
- **Server-side session store (Redis).** Starlette's signed-cookie session is sufficient single-tenant; a
  server store is a later option if session size / revocation needs it.

## Consequences
- The app authenticates browsers itself (no proxy) **and** still serves bearer API/agent callers — one app,
  two auth paths into one `Principal`. Unblocks the clickable Integrations UI (slice 4b).
- **New deps** (`authlib`, `itsdangerous`) + **new secrets** (`session_secret_key`, `zitadel_client_secret`)
  guarded fail-closed in prod. The OIDC flow is **state+PKCE+nonce** protected and has **no open redirect**.
- **Not person-affecting** (authn, not ER/merge/score). **No migration. No new datastore. Single-tenant.**

## Reversibility
Reversible — drop the session middleware + auth router + the dual-path branch (the bearer path is untouched).
Reversal cost: low-medium. Revisit triggers: session revocation/size → server-side store; many web routes →
a dedicated `require_session` dependency; proxy chosen later → keep bearer-only.

## Invariant gate note
Authn boundary — not an ER/merge/provenance invariant, so no `@given`. **Security failing-test-first
(the headline):** (a) BEARER PATH UNCHANGED — valid bearer → principal, invalid/missing bearer → 401 for a
JSON/API request (every existing api-auth test stays green); (b) SESSION PATH — a request with a valid signed
session principal authenticates; a tampered/absent session does not; (c) `/login` 302s to the Zitadel
authorize endpoint carrying `response_type=code` + `state` + a PKCE `code_challenge`; (d) `/auth/callback`
with an injected fake OAuth stores the principal in the session and 302s to the validated `next`; a
mismatched OAuth state is rejected; (e) NO OPEN REDIRECT — `next=//evil.com` / `next=https://evil.com` is
refused, only a local `/path` is honoured; (f) the session cookie is `httponly` + `samesite=lax` (+ secure
outside dev); (g) `validate_production_secrets` fails closed on a placeholder `session_secret_key` outside
dev/test. All over an injected fake OAuth + fake verifier — **no live Zitadel, no network**.
