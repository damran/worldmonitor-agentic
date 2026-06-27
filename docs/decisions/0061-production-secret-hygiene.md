# 0061 — Production secret hygiene: loopback-bound stores + fail-closed placeholder secrets

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** Stage-0 / audit **M-2** (`gate/m2-secret-hygiene`). Off `master`.
- **Addresses:** audit M-2 — weak/placeholder secrets + backend ports exposed to the host.

## Context

The audit (M-2) flagged three things: an all-zeros `ZITADEL_MASTERKEY`, guessable service passwords, and
compose **publishing backend ports** (5432/7687/9000/6379/8080) to the host. Current state on inspection:

- `deploy/compose.yaml` already **requires** every secret via `${VAR:?set ... in .env}` (no hardcoded
  weak default) and `.env.example` ships `change-me` placeholders (not weak real values) — so the worst
  of M-2 is already mitigated. Two real gaps remain:
- Backend store services publish their ports on **all interfaces** (`ports: ["5432:5432"]` etc.), so a
  store is reachable from the network, not just the host.
- Nothing **fails closed** when a deployment is run in a non-`development` environment with a *placeholder*
  secret still in place (`config_encryption_key=change-me`, a `change-me` DB password) — it would limp
  until a later opaque failure.

The `ZITADEL_MASTERKEY` is **Zitadel's** (passed to the zitadel service via compose `--masterkey`), not a
worldmonitor `Settings` field — the app cannot validate it. It is enforced at the deploy layer
(compose `:?` requires it; `.env.example` gets a "generate with `openssl rand`" hint).

## Decision

1. **Loopback-bind the backend stores.** In `deploy/compose.yaml`, publish postgres/neo4j/minio/redis
   ports as `127.0.0.1:<port>:<port>` (host-loopback only) instead of `<port>:<port>` (all interfaces).
   The app/driver/zitadel reach them over the compose network by service name regardless; only host
   access narrows to loopback. (api/zitadel host exposure unchanged — they are the front doors.)
2. **Fail-closed on placeholder secrets in non-dev.** Add `Settings.validate_production_secrets()` that,
   when `environment != "development"`, raises `ValueError` if a secret the app reads is a recognizable
   placeholder/weak value — `config_encryption_key` empty or `change-me`; a `POSTGRES_DSN` /
   `redis_url` / password containing `change-me` or a known-guessable token (e.g. `worldmonitor123`).
   Call it from `api.main.create_app` and the driver entrypoint (`runner.driver`) so a misconfigured
   non-dev boot halts loud. Development is unaffected (placeholders allowed locally).
3. **`.env.example` tidy.** `MINIO_ROOT_USER=worldmonitor` → `change-me`; add a one-line
   `ZITADEL_MASTERKEY` generation hint (`openssl rand -hex 16`) and a note that non-dev boots reject
   `change-me` placeholders.

## Alternatives considered
- **Add a `zitadel_masterkey` Settings field + validate it.** The app never uses the masterkey; adding a
  field it ignores invites drift. Enforce at the deploy layer (compose `:?` + the .env.example hint).
- **Remove backend host port publishing entirely.** Breaks local dev/debugging (psql/cypher-shell from
  the host). Loopback-binding keeps host dev access without network exposure. Chosen.
- **Entropy-check secrets (length/charset).** Over-fits; a determined-weak strong-looking password passes
  anyway. Placeholder-marker rejection catches the realistic "forgot to replace change-me" footgun.

## Consequences
- Backend stores are no longer reachable off-host; a non-dev deploy that forgot to set real secrets
  halts at startup with a clear message instead of running insecure or failing opaquely later.
- No migration; no schema change. Not person-affecting (deploy/secret hygiene). `human_fork: false`.

## Reversibility
Reversible (deploy + a startup check). Reversal cost: low. Revisit trigger: move secret management to a
vault/secrets-manager (then the placeholder check is replaced by the vault's presence guarantees).

## Invariant gate note
Not an ER/provenance invariant → no `@given` required. Failing-test-first: a unit test asserting
`validate_production_secrets()` raises in a non-dev environment with a placeholder secret and passes for
development / strong secrets; plus a compose-parse guard test asserting backend ports are loopback-bound.
