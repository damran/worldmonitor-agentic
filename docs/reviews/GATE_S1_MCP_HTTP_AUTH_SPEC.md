# GATE S1 — MCP HTTP transport + Zitadel bearer auth

> Phase-3 slice S1 (the security boundary). ADRs: **0090** (this gate) under umbrella **0089**.
> Realizes ADR 0063's named revisit trigger ("remote Hermes → HTTP + bearer"). **You write code to
> this spec; do not relitigate the ADR decisions.** Builder confirms exact `mcp==1.28.1` API surface
> at build time.

## 1. Goal (one sentence)
Give the existing 4-tool, read-only MCP server an **authenticated `streamable-http` transport** gated
by the **same** Zitadel bearer verification REST already uses, so a **remote** Hermes can connect —
without regressing the stdio path and without exposing any anonymous network surface.

## 2. Scope — exact files (also `.claude/gate.scope`)
**In scope (touch only these):**
- `src/worldmonitor/mcp/server.py` — add config-selected transport; HTTP branch wires auth. stdio
  branch byte-behaviour-preserved.
- `src/worldmonitor/mcp/auth.py` — **NEW.** The `TokenVerifier`-Protocol adapter around
  `authz.oidc.ZitadelTokenVerifier`; claims→`AccessToken`(scopes) mapping; `AuthSettings` builder.
- `src/worldmonitor/authz/oidc.py` — **only if** a claims-helper is genuinely needed (e.g. extract the
  Zitadel project-role claim). Prefer leaving it untouched; do not change `verify()` semantics.
- `src/worldmonitor/settings.py` — **only** the MCP transport/host/port/auth settings fields (additive;
  defaults keep stdio + auth-required-when-HTTP). _(Listed in gate.scope; keep the diff minimal.)_
- `scripts/dev/zitadel_provision.sh` — create the `worldmonitor:graph-read` role + grant it to the
  `hermes` principal + document token acquisition.
- `tests/unit/test_mcp_http_auth.py`, `tests/property/test_mcp_auth_boundary.py`,
  `tests/integration/test_mcp_http_transport.py` — **NEW** (names below).
- `docs/decisions/0089-hermes-agent-layer.md`, `docs/decisions/0090-mcp-http-bearer-auth.md`,
  `docs/reviews/GATE_S1_MCP_HTTP_AUTH_SPEC.md` — these documents.

**Out of scope (do NOT touch):** `api/graph.py`, `graph/queries.py`, `graph/read_guards.py` (frozen —
behaviour reuse only), any S2 LLM file (`src/worldmonitor/llm/`), any Hermes compose/deploy file (S3),
any new MCP tool, any write/active path. **No new MCP tool. No change to the 4 tool bodies.**

## 3. Locked invariants (the S1 contract)
- **INV-S1-AUTH — auth required on the HTTP transport.** Every tool call over HTTP MUST carry a valid
  Zitadel bearer (verified by `ZitadelTokenVerifier`). Missing / malformed / wrong-issuer /
  wrong-audience / expired token → **rejected before any tool body runs**. No anonymous access on the
  network surface. The HTTP app is **never constructed without the verifier** (fail-closed startup).
- **INV-S1-ROLE — service-principal scoping.** Only a principal bearing the **`worldmonitor:graph-read`**
  role (read + run-passive) is authorized; a valid token lacking that role → **403**. Token validity is
  necessary; the role is the authorization.
- **INV-S1-READONLY — read-only preserved.** The HTTP surface exposes **only** the 4 existing read
  tools (`get_entity`, `get_neighbors`, `get_provenance`, `find_paths`); no new tools; `execute_read`
  only; the shared `read_guards` hop-clamp + id-validation still apply unchanged.
- **INV-S1-STDIO — stdio retained.** The existing stdio transport keeps working **unchanged** (no auth,
  stdout-purity intact); transport is config-selected; the stdio default path is byte-behaviour-equal
  to ADR 0063.
- **INV-S1-NOLEAK — no secret/PII in errors.** Auth failures return generic **401**/**403**
  (`{"error","error_description"}` + `WWW-Authenticate: Bearer`); responses never contain token bytes,
  claim values, JWKS internals, or stack traces.
- **Carried invariants (must not regress):** G1 provenance-on-every-node-and-edge (the read tools still
  surface `prov_*`); append-only / canonical-canonical-only-via-guard are unaffected (read surface, no
  writes). Stdout-purity remains a hard invariant on the **stdio** path.

## 4. Primary test mandate (security boundary → strong primary test)
This gate touches an auth/security boundary, so the primary test is **property/metamorphic-flavored**,
backed by example tests for the named invariants.

### 4a. PRIMARY — `tests/property/test_mcp_auth_boundary.py` (`@given`)
Generate header/token permutations and assert the boundary holds for **every** one:
- **Generators:** Authorization header ∈ {absent, empty, `"Bearer"` no-token, `"Bearer <garbage>"`,
  non-Bearer scheme, valid JWT but wrong-issuer, wrong-audience, expired, well-formed valid **without**
  the role, well-formed valid **with** the role}. Drive the SDK `BearerAuthBackend` + the adapter with a
  **fake `ZitadelTokenVerifier`** (deterministic: signs/accepts the test keypair; no network).
- **Property (the headline):** *no request lacking a valid-bearer-with-role ever reaches a tool body.*
  Assert with a **spy tool** (a sentinel that records invocation): for every non-(valid+role) input the
  spy is **never called** and the response is 401 (no/invalid token) or 403 (valid, no role); only the
  valid+role input reaches the spy.
- **Property (no-leak):** for every rejected input, the response body contains **none** of: the raw
  token string, any claim value, the substring `Traceback`. (metamorphic: token bytes in ⇒ token bytes
  never out.)
- **Property (clamp survives auth):** an authorized `get_neighbors`/`find_paths` call with a huge/
  negative/zero hop still clamps to `read_guards.HOP_CAP` (auth layer doesn't bypass the guard).
- Use `deadline=None` if JWKS-fake setup makes per-example timing flaky (see the builder-flake memory).

### 4b. Example tests — `tests/unit/test_mcp_http_auth.py`
- **INV-S1-ROLE:** valid token **with** `worldmonitor:graph-read` → tool runs; valid token **without**
  it → 403 `insufficient_scope`; tool body not entered.
- **INV-S1-AUTH fail-closed startup:** building/serving the HTTP transport **without** an auth verifier/
  settings **raises** at construction (no anonymous port can be opened).
- **adapter mapping:** `verify_token` returns `None` on `InvalidTokenError`; returns an `AccessToken`
  whose `scopes` contain `worldmonitor:read` exactly when the role claim is present; `client_id`/
  `expires_at` populated from `sub`/`exp`.
- **INV-S1-NOLEAK:** 401 and 403 bodies asserted to exclude token/claim/traceback substrings.

### 4c. Example tests — read-only + stdio non-regression
- **INV-S1-READONLY:** the HTTP server registers **exactly** `{get_entity, get_neighbors,
  get_provenance, find_paths}` — assert the registered tool set equals that set (no more, no less).
- **INV-S1-STDIO:** the existing ADR 0063 stdio tests still pass unchanged; add one asserting the
  stdio transport path constructs **without** requiring auth settings (no regression).

### 4d. Integration — `tests/integration/test_mcp_http_transport.py`
- Stand up `streamable_http_app()` with the adapter over a fake/in-proc verifier (no live Zitadel);
  send a real HTTP `tools/call` with: (i) no header → 401; (ii) valid bearer + role → tool result
  carrying `prov_*`. Confirms end-to-end wiring (BearerAuthBackend → RequireAuthMiddleware → tool).
- Docker is available here — run it locally; do not defer to CI-only.

## 5. Acceptance criteria (all must be green)
1. `tests/property/test_mcp_auth_boundary.py` passes: the headline property holds over all generated
   permutations (no unauthorized request reaches a tool; no token bytes leak).
2. INV-S1-ROLE, INV-S1-AUTH, INV-S1-READONLY, INV-S1-STDIO, INV-S1-NOLEAK each have a passing named test.
3. The HTTP transport cannot be served without the bearer verifier (fail-closed startup test green).
4. Exactly the 4 read tools on the HTTP surface; no write/active tool added.
5. The ADR-0063 stdio suite stays green (byte-behaviour preserved).
6. `zitadel_provision.sh` creates `worldmonitor:graph-read`, grants it to `hermes`, and documents token
   acquisition (reviewed for shell-safety: no secret echoed, no interpolation of untrusted data).
7. Ruff + Pyright(strict on `src/`) clean; `ruff format --check .` clean repo-wide; CI `quality` +
   `security` green before merge.

## 6. Sub-slice breakdown (S1 may split into two individually-mergeable PRs)
- **S1a — transport seam.** Config-selected transport in `server.py`: stdio (default, unchanged) vs
  `streamable-http`; settings fields; the HTTP branch serves `streamable_http_app()`. Lands with the
  read-only + stdio-non-regression tests (4c) and a **placeholder fail-closed guard** that refuses to
  serve HTTP until auth is wired (so S1a never ships an anonymous port). Mergeable alone.
- **S1b — auth + role.** `mcp/auth.py` adapter + `AuthSettings(required_scopes=["worldmonitor:read"])`
  + the `zitadel_provision.sh` role + the PRIMARY property test (4a), the auth/role/no-leak unit tests
  (4b), and the integration test (4d). Flips the S1a guard to "HTTP requires a verifier." Mergeable
  after S1a.

Each sub-slice ships its own tests and is green on its own. If built as one PR, all of §4 lands together.

## 7. Notes for the builder
- The pinned `mcp==1.28.1` bundles `mcp.server.auth` (`TokenVerifier`, `BearerAuthBackend`,
  `RequireAuthMiddleware`, `AuthSettings`) and `FastMCP(auth=…, token_verifier=…)` →
  `streamable_http_app()` wires it. **Use the native path; do not hand-roll middleware.** Fallback
  (only if a future pin drops the module): a Starlette `AuthenticationMiddleware` with the same
  `BearerAuthBackend` shape — documented in ADR 0090, not to be built now.
- Confirm the exact Zitadel role claim path/audience at build time (project roles surface under
  `urn:zitadel:iam:org:project:roles`); keep the role→scope map in `mcp/auth.py`, one place.
- Treat the inbound token as hostile: never log it, never echo it, no `eval`/interpolation.
</content>
