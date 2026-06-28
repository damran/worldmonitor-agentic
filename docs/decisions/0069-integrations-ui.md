# 0069 — Integrations UI: HTMX + Jinja2 catalog + schema-driven config (Phase-2 Stage-3 slice 4b)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/3e-integrations-ui` (off `master`). Builds on the session-auth foundation (ADR 0068).
- **Milestone:** Phase 2 (`docs/40_ROADMAP.md`) — this slice CLOSES the Phase-2 acceptance loop: *add a
  source from the UI → it's enabled → the driver collects it → query it via REST + MCP*.
- **human_fork:** false (the stack was chosen by the user — HTMX + Jinja2 + FastAPI; the rest is reversible).

## Context

The plugin framework, the read surface (REST + MCP), four connector/notifier plugins, and browser session
auth (ADR 0068) are all shipped. What's missing is the **operator UI**: a catalog of plugins + a
schema-driven config form that saves a `ConnectorInstance` (the **first write path** — nothing creates
instances today; the driver only reads `status="enabled"` ones). The user chose **HTMX + Jinja2 +
FastAPI** (server-rendered, Python-only, no SPA toolchain — `docs/30` Airbyte "spec-drives-form" pattern).

## Decision

**Server-rendered, auth-gated HTML routes in a new `api/integrations.py` router, rendering a plugin catalog
+ a form generated from each plugin's `config.schema.json`, saving an encrypted `ConnectorInstance`.**

### Routes (all behind `get_principal`; an unauthenticated browser is 302'd to `/login` by the dual-path middleware, ADR 0068)
- `GET /integrations` — the catalog + instances page: available plugins from `Registry.all_manifests()`
  (connectors **and** notifiers — name/kind/description/status + an "Add" link), and existing
  `ConnectorInstance`s with `status` / `last_run` / `next_run` + their latest `TaskRun` outcome+error, plus
  enable/disable buttons. Mints a CSRF token into the session for the action forms.
- `GET /integrations/new/{plugin_id}` — a **schema-driven** config form from `registry.get*(plugin_id).config_schema`:
  `string`→text, `integer`/`number`→number, `boolean`→checkbox, `enum`→`<select>`, **`"secret": true`**→
  `<input type=password>` (rendered EMPTY). Required fields marked. 404 for an unknown `plugin_id`.
- `POST /integrations` — create: **validate CSRF** → resolve `plugin_id` (404 if unknown) → build the config
  dict from the form → `plugin.validate_config(config)` (422 on a bad config) → `ConfigCipher.encrypt(json)` →
  insert `ConnectorInstance(id=uuid, connector_id, config_encrypted, status="enabled", next_run=NULL)` → 303
  to `/integrations`. (`status="enabled"` so the driver collects it next tick — the acceptance flow.)
- `POST /integrations/instances/{id}/enable` and `/disable` — **validate CSRF** → flip `status`
  enabled↔disabled (the safe enable/disable path — `disabled` is simply skipped by `run_due_ingests`) → 303.

### Wiring
- `create_app(*, db_sessions: sessionmaker | None = None, registry: Registry | None = None, ...)` — inject a
  Postgres sessionmaker (default `session_factory(engine_from_settings(settings))`, the readiness pattern) and
  the plugin Registry (default a discovered one). A `get_db` dependency yields a session per request. Tests
  inject a testcontainer sessionmaker + a Registry with fakes.
- `Jinja2Templates(directory=…api/templates)` + `StaticFiles` mounted at `/static`. New explicit deps:
  `jinja2`, `python-multipart` (FastAPI form parsing).

### Security (a browser form UI with cookie session auth — the threats this slice introduces)
- **Authz:** every route `get_principal`-gated; state-changing POSTs require an authenticated session.
- **CSRF:** a **synchronizer token** — minted into the session on a form GET, embedded as a hidden field,
  and **required + compared** on every POST (mismatch/absent → **403**). Defense-in-depth on top of the
  session cookie's `SameSite=lax` (which already blocks cross-site cookie-bearing POSTs).
- **XSS:** Jinja2 **autoescaping** (on by default for `.html`) escapes all rendered manifest/config/instance
  data; no `| safe` on untrusted data.
- **Secrets:** config secrets are `ConfigCipher`-encrypted before storage, **never logged**, and **never
  rendered back** — v1 is **create-only** (no edit form pre-filled with decrypted secrets); secret inputs
  render empty.
- **Input validation:** unknown `plugin_id` → 404; config validated via `plugin.validate_config` (the
  plugin's JSON Schema) **before** encrypt+store; the instance id is a server-generated uuid.
- **No graph write / no resolution** — the UI only reads the registry + writes a `ConnectorInstance` row.

### Folded-in hardening (the ADR-0068 follow-up)
`auth_web._is_safe_next` is tightened to reject **all** non-printable / non-ASCII chars (C1 controls,
Unicode line/paragraph separators ` `/` `/`\x85`) — making the open-redirect guard self-sufficient
now that `next` flows back into the rendered UI navigation.

## Alternatives considered
- **React SPA / @rjsf.** The user chose server-rendered HTMX (lower ops, no Node). React is reserved for a
  later graph explorer (roadmap).
- **Edit existing instance config in v1.** Deferred — re-rendering a form with decrypted secrets is a leak
  risk; create-only + enable/disable + (later) delete is the safe v1.
- **Rely on `SameSite=lax` alone for CSRF.** It blocks cross-site cookie POSTs, but a synchronizer token is
  the correct, defense-in-depth posture for a form UI; both are used.

## Consequences
- The Phase-2 acceptance loop is closeable end-to-end: add a source in the browser (Zitadel-authed) → it's
  enabled → the driver collects → query via REST + MCP. The catalog lists connectors + notifiers uniformly.
- New deps (`jinja2`, `python-multipart`) + templates/static dirs + a Postgres sessionmaker in `create_app`.
- **Not person-affecting** (operator config; no ER/merge/score). **No migration** (`ConnectorInstance`
  exists). **No new datastore. Single-tenant** (D1 — no tenant scoping).

## Reversibility
Reversible — drop the router + templates + the create_app injections. Reversal cost: low-medium. Revisit
triggers: edit-config need → a masked-secret edit flow; live status → HTMX polling / SSE; a graph explorer →
the React app (roadmap); rich validation UX → @rjsf.

## Invariant gate note
A read+config-write UI — not an ER/merge/provenance invariant, so no `@given`. **Security failing-test-first:**
(a) every route `get_principal`-gated (unauthenticated → 401/redirect, NOT served); (b) a POST without/with a
wrong **CSRF** token → 403 (no instance created / no status change); (c) the saved `ConnectorInstance` stores
**`config_encrypted`** (the raw secret is NOT in the row, and `ConfigCipher.decrypt` round-trips it); (d) an
unknown `plugin_id` → 404 and a config failing `validate_config` → 422 (no row written); (e) secrets are
**never echoed** into a rendered form nor logged; (f) a secret field renders as `type=password`, empty;
(g) enable/disable flips `status` and is CSRF-gated; (h) `_is_safe_next` rejects the Unicode/C1 separators.
All over a testcontainer Postgres + an injected Registry + the session-auth test pattern (no live Zitadel).
