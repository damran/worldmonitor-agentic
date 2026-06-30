# 0090 — Phase-3 S1: authenticated HTTP transport for the MCP read server

- **Status:** PROPOSED (2026-06-30)
- **Date:** 2026-06-30
- **Gate:** Phase-3 slice **S1** (the security boundary). Companion spec:
  `docs/reviews/GATE_S1_MCP_HTTP_AUTH_SPEC.md`. Umbrella: ADR 0089 (D2).
- **Milestone:** Phase 3 (`docs/40_ROADMAP.md:71`). Prerequisite for S3/S4 (remote Hermes connect).
- **human_fork:** false — reversible; reuses primitives already in the tree.
- **Realizes:** ADR 0063 §1 + §3 revisit trigger ("a remote/non-co-located Hermes → the server **must**
  adopt the same Zitadel bearer verification REST uses before binding a port").

## Context

ADR 0063 shipped the MCP read server over **stdio** with exactly four read-only, bounded,
parameterized tools (`get_entity` / `get_neighbors` / `get_provenance` / `find_paths`), and stated its
own reversal: when Hermes goes remote, flip to HTTP **and** add bearer auth — *never expose a port
unauthenticated*. ADR 0089 D2 fires that trigger. The auth primitive already exists
(`authz/oidc.py::ZitadelTokenVerifier`, RS256/JWKS) and the REST surface (`api/graph.py`) already
gates every read behind a verified Zitadel bearer via `get_principal`. S1 brings the MCP HTTP surface
to the **same** bar, reusing the same verifier.

**FastMCP version supports bearer auth natively.** Pinned `mcp==1.28.1` (`mcp>=1.28` in
`pyproject.toml`) bundles `mcp.server.auth` with:
- `TokenVerifier` Protocol — `async def verify_token(self, token: str) -> AccessToken | None`;
- `BearerAuthBackend` — extracts `Authorization: Bearer <t>`, calls the verifier;
- `RequireAuthMiddleware` — rejects a missing/invalid token with **401 `invalid_token`** and a token
  lacking a required scope with **403 `insufficient_scope`**, both as generic JSON
  `{"error","error_description"}` + a `WWW-Authenticate: Bearer` header (**no token contents echoed**);
- `FastMCP(auth=AuthSettings(issuer_url=..., required_scopes=[...]), token_verifier=<v>)` →
  `streamable_http_app()` wires `BearerAuthBackend` + `RequireAuthMiddleware` in front of every tool
  call automatically.

So S1 needs **no hand-rolled auth middleware**: it provides a thin adapter mapping our existing
`ZitadelTokenVerifier` to the SDK's async `TokenVerifier` Protocol, plus an `AuthSettings` carrying the
read+run-passive required scope. (If a future pin drops `mcp.server.auth`, the fallback is a Starlette
`AuthenticationMiddleware` with the same `BearerAuthBackend` shape — recorded as an alternative, not
built.)

## Decision

**Add an HTTP (`streamable-http`) transport to the existing MCP read server, gated by the same Zitadel
bearer verification as REST, selected by config; keep the stdio transport unchanged.** Concretely:

### 1. A token-verifier adapter — reuse, don't reimplement (reversible)
A new `src/worldmonitor/mcp/auth.py` exposes an adapter implementing the SDK `TokenVerifier` Protocol
around the existing `authz.oidc.ZitadelTokenVerifier`:
- `async verify_token(token)` calls the existing sync `verify(token)` (JWKS/RS256/issuer/audience).
- On `InvalidTokenError` → return **`None`** (→ 401, before any tool runs).
- On success → map claims → `AccessToken(token=…, client_id=sub, scopes=<roles→scopes>, expires_at=exp)`.
- **Role → scope mapping:** the Zitadel **project role** granted to the `hermes` principal (S1 names it
  `worldmonitor:graph-read`, the **read + run-passive** role) is read from the standard Zitadel role
  claim (`urn:zitadel:iam:org:project:roles`, builder confirms the exact claim path/audience at build
  time) and mapped to the scope `worldmonitor:read`. A token without that role yields no matching
  scope → **403** at `RequireAuthMiddleware`.
- The adapter performs **no I/O of its own** beyond the wrapped verifier (JWKS cache lives in
  `ZitadelTokenVerifier`); it is unit-testable with a fake verifier.

### 2. Transport selection by config (reversible)
`build_server(...)` stays transport-agnostic. A settings flag selects the transport at run/serve time:
- `stdio` (default, unchanged — local/admin/fallback): `run(transport="stdio")`, no auth, no port,
  stdout-purity invariant preserved exactly as ADR 0063.
- `streamable-http`: constructs `FastMCP(..., auth=AuthSettings(issuer_url, required_scopes=
  ["worldmonitor:read"]), token_verifier=<adapter>)` and serves `streamable_http_app()` on a configured
  host/port. **The HTTP app is never constructed without the verifier** (fail-closed: enabling HTTP
  requires the auth config to be present, else startup raises — no accidental anonymous port).

### 3. Tool set unchanged — read-only preserved (reversible/additive)
The HTTP surface registers **exactly the same four read tools** via the same `build_server`. No new
tool; `execute_read` only; the shared `read_guards` hop-clamp + id-validation still apply (one cap, one
place). HTTP adds **no** write/active/enrich/score tool (those are Phase 6, ADR 0089).

### 4. Service-principal scoping documented (reversible)
`scripts/dev/zitadel_provision.sh` is extended to (a) create the `worldmonitor:graph-read` project
role and (b) grant it to the `hermes` service principal, and to document how Hermes obtains a token
(client-credentials / JWT-profile for the machine user) to put in its MCP `Authorization: Bearer`
header. Token validity is **necessary**; the **role** is the authorization.

## Alternatives considered
- **Hand-rolled Starlette `AuthenticationMiddleware`.** Unnecessary — the pinned SDK ships
  `BearerAuthBackend` + `RequireAuthMiddleware`. Kept as the documented fallback if a future pin drops
  `mcp.server.auth`.
- **Swap to standalone `fastmcp` 3.x for its auth providers.** ADR 0063 §5 already chose the lean
  official SDK; it covers bearer auth natively, so the 34-package tail is still unjustified.
- **Co-located stdio only (no HTTP).** Rejected by the user (ADR 0089 D2) — Hermes is remote.
- **A bespoke API token / shared secret instead of OIDC.** Rejected — would fork the auth model; reuse
  the single Zitadel bearer path REST already uses.

## Consequences
- One new authenticated network ingress (the MCP HTTP port). Secured **before** it is exposed.
- New module `src/worldmonitor/mcp/auth.py`; additive change to `mcp/server.py` (transport selection)
  and `scripts/dev/zitadel_provision.sh` (role). stdio path byte-behaviour-preserved.
- The security boundary for S3/S4 (remote Hermes) is in place; ADR 0063's revisit trigger is closed.
- Not person-affecting (read surface). No new datastore. Single-tenant (D1/ADR 0042).

## Invariant gate note
S1 is an **auth/security boundary**, so per CLAUDE.md build-discipline it carries a **strong primary
test**, expressed property-style: *over generated header/token permutations (absent / malformed /
wrong-issuer / wrong-audience / expired / valid-but-no-role / valid-with-role), no request without a
valid bearer-with-role ever reaches a tool body.* Plus example tests for role-scoping (403 without the
role), read-only preservation (only the 4 tools; stdio unchanged), and no-secret-in-error (401/403
bodies never contain token bytes). Exact list in `GATE_S1_MCP_HTTP_AUTH_SPEC.md`.

## Reversibility
Reversible: drop the HTTP branch + `mcp/auth.py`; stdio stands alone. Reversal cost low; revisit
trigger = Hermes co-located again (stdio default returns). No data-shape lock-in, no deletion — **no
human fork**.
</content>
