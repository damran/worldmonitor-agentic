# 0067 — Notifier plugin type + TelegramNotifier (Phase-2 Stage-3 slice 3)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/3c-telegram-notifier` (off `master`). Third Stage-3 slice.
- **human_fork:** false (defines a plugin type with sensible-default, reversible design choices; no OPEN
  decision introduced).

## Context

`Kind.NOTIFIER` exists in the plugin enum and `plugins/notifiers/` is a one-line stub, but there is **no
`Notifier` base class** — only `Connector`. The `Manifest` dataclass requires connector-only `mode` +
`capability`, and the `Registry` discovers/serves `Connector`s only. So the first notifier
(**TelegramNotifier**, for deterministic "rule fired / run complete" alerts — the plan's notifier slice;
distinct from the H-8 **metrics** path which ADR 0054 routed to Prometheus `/metrics`) must first establish
the **Notifier plugin type**. This slice defines that contract and ships Telegram as its first instance.

## Decision

### 1. The `Notifier` plugin type (`plugins/base.py`)
- **`Notification`** — a small frozen dataclass, the channel-agnostic payload a notifier renders:
  `title: str`, `body: str`, `severity: str = "info"` (info/warning/critical), `context: Mapping[str,str]
  = {}`.
- **`Notifier(ABC)`** — mirrors `Connector`'s shape: `manifest -> Manifest`, `config_schema -> dict`,
  `send(self, config: Mapping, notification: Notification) -> None` (deliver; raises on failure), and the
  same `validate_config` (jsonschema). No `collect`/`map` — a notifier is a sink, not a source.
- **`Manifest.mode` / `Manifest.capability` become `Optional[...] = None`** — they are connector concepts;
  a notifier manifest sets `kind=NOTIFIER`, `mode=None`, `capability=None`. Backward-compatible: every
  connector still passes both. (The id field stays named `connector_id`, reused as the plugin id — a known
  naming wart; a future `PluginManifest` refactor can rename to `plugin_id`. Out of scope here.)

### 2. Registry — additive notifier support (`plugins/registry.py`)
Keep the **connector** path byte-for-byte unchanged (the ingest driver + every connector test depend on
`register`/`get`/`all`/`manifests`). **Add parallel notifier methods**: `register_notifier`, `get_notifier`,
`all_notifiers`, `notifier_manifests`, and a combined `all_manifests()` (connectors + notifiers) for the
Integrations-UI catalog. `discover_module` is extended to also register concrete `Notifier` subclasses (into
the notifier side). No shared `Plugin` base is introduced yet (a later refactor can unify) — this is the
lowest-blast-radius change that leaves the connector/driver path frozen.

### 3. TelegramNotifier (`plugins/notifiers/telegram/`)
`notifier.py` (subclasses `Notifier`) + `config.schema.json` + `__init__.py`, auto-discovered.
- **Manifest:** `connector_id="telegram"`, `kind=NOTIFIER`, `mode=None`, `capability=None`,
  `status=IMPLEMENTED`.
- **`config.schema.json`** (`additionalProperties:false`): `bot_token` (string, **`"secret": true`** —
  vault-encrypted at rest), `chat_id` (string), optional `parse_mode` (enum `["MarkdownV2","HTML"]`).
  `required: ["bot_token","chat_id"]`.
- **`send(config, notification)`**: render `notification` → message text (title + body, severity-prefixed),
  build `https://api.telegram.org/bot<bot_token>/sendMessage` with `chat_id` + `text` (+ `parse_mode`) as
  **query params** (Telegram supports GET; `guarded_stream` has no request-body param) → fetch via
  `guarded_stream("GET", url, transport=self._transport)` → `raise_for_status` → read the small response
  **bounded** (byte cap). Injectable `transport` ctor kwarg (for `httpx.MockTransport` tests).
- **Secret hygiene:** the `bot_token` rides in the URL **path** (`/bot<token>/...`). httpx's INFO request-URL
  logging is already suppressed at the egress chokepoint (`net/ssrf.py::_quiet_http_request_logging`, ADR
  0065), so the token can't leak there; the notifier additionally **never logs the token or the URL** (logs
  only `chat_id` + outcome). Locked by an all-loggers token-not-logged test.

### 4. Deliberately deferred (decision-free v1)
- **Trigger wiring** (rule-fired / run-complete → `notifier.send(...)`): the rules/scoring layer that fires
  alerts isn't built yet; v1 ships the notifier **plugin + `send` interface**, tested in isolation, ready to
  be invoked. No change to the run path.
- **Retry/rate-limit** on send: a single attempt; failures raise (the future caller decides retry). Telegram
  rate-limit backoff deferred.
- **Other channels** (Slack, email, webhook): later notifier plugins against the same `Notifier` contract.

## Alternatives considered
- **A shared `Plugin` base for Connector + Notifier + a unified registry.** Cleaner long-term typing, but a
  larger refactor touching `Connector`, the registry's connector methods, and the driver's `get`. Deferred —
  the additive parallel-methods approach keeps the connector path frozen and is reversible.
- **POST with a JSON body to Telegram.** `guarded_stream` streams a bodyless request; Telegram's GET form
  with query params is sufficient for v1 and reuses the SSRF guard unchanged.
- **A separate `NotifierManifest` type.** Avoids the optional connector fields, but ripples the `Manifest`
  type through every connector. Reusing `Manifest` with optional fields is the minimal change.

## Consequences
- The platform gains its first **notifier** plugin type + a working Telegram sink — the deterministic-alert
  channel. The Integrations-UI catalog (later) lists notifiers alongside connectors via `all_manifests()`.
- **New attack surface handled:** the bot_token is a config secret (encrypted at rest) and is kept out of
  logs (egress suppression + the notifier's own redaction). All egress via `guarded_stream` (SSRF-safe).
- **Not person-affecting** (an alert sink — no ER/merge/score). **No migration. No new datastore.
  Single-tenant.** Connector/driver path unchanged.

## Reversibility
Reversible — a removable plugin + additive base/registry surface. Reversal cost: low. Revisit triggers:
many plugin kinds → unify under a `Plugin` base + manifest rename; send reliability needed → add retry/queue;
more channels → more notifier plugins.

## Invariant gate note
A notifier neither resolves/merges nor writes the graph, so no ER/merge/canonical-id invariant is touched →
no `@given` mandatory. Failing-test-first: the `Notifier` ABC + `Notification` exist; `Manifest` accepts
`mode=None`/`capability=None` (and connectors still construct with both); the registry discovers + serves a
notifier (`notifier_manifests` / `get_notifier`) without disturbing connector discovery; TelegramNotifier's
manifest is `kind=NOTIFIER`; its config schema marks `bot_token` secret + requires `chat_id`; `send()` over
`httpx.MockTransport` issues the Telegram `sendMessage` request carrying `chat_id` + the message text, raises
on a Telegram error, fetches only via `guarded_stream` (private host blocked), and **never logs the
bot_token** (all-loggers capture) — no live HTTP.
