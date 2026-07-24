# 0127 — Thin read-only `wm` CLI over our REST (slice 1: `health` / `ready` / `entity`)

- **Status:** PROPOSED
- **Date:** 2026-07-24
- **human_fork:** false — a thin, read-only, additive command-line client over **existing** REST
  endpoints. No product/architecture fork; it introduces no new server behaviour, datastore, or
  contract. Every scoping choice below (module location, env-var names, exit codes, the `ready` 503
  mapping, JSON-to-stdout) is reversible with a cheap, recorded revisit trigger. Not marked OPEN.
- **person_affecting:** false — see "Person-affecting reasoning". The CLI performs **no** write, ER,
  merge, resolution, scoring, inference, or model/param promotion; it makes **no** change to the live
  system; it surfaces the **same** bytes `GET /entities/{id}` already returns to an authenticated
  caller — no new exposure. No cosign required.
- **human_cosign:** not required — reversible, non-person-affecting, read-only client (cost directive:
  reserve cosign for irreversible / person-affecting changes).
- **Backlog/roadmap:** `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-6** (P1 / S-M). This
  ADR covers **slice 1 only** — the three commands; noun expansion is slice 2 (listed under
  "Consequences"). F-12 (zero-dep importable client) stays a separate, demand-driven gate.
- **Spec:** `docs/reviews/GATE_F6_WM_CLI_SPEC.md`.
- **Builds on:** ADR 0062 (REST read routes: `GET /entities/{id}`), 0051 (`/ready` fail-closed store
  probe), 0059 (driver-heartbeat is non-fatal observability on `/ready`), 0068 (bearer vs session
  auth; the frozen 401 for JSON callers), 0090/0093 (Zitadel service-principal bearers, `WM_MCP_TOKEN`
  / `WM_LLM_TOKEN` naming precedent), 0042 (single-tenant).

## Context

Row F-6 asks for a **thin read-only `wm` CLI** over **our** REST — `[project.scripts] wm`, driven by
`WM_BASE_URL` + a bearer, with an **exit-code contract**, "noun expansion later". Slice 1 is exactly
`wm health / ready / entity <id>` + tests. The three endpoints already exist and are stable:

- `GET /health` — **public**, always `200`, `{"status":"ok","environment":…}`.
- `GET /ready` — **public**, `{"ready":bool,"checks":{…}}`, HTTP `200` iff ready else `503`
  (fail-closed, stores-only); `checks.driver` is **non-fatal observability** (`ok`/`stale`/`unknown`),
  never flips the status (ADR 0059).
- `GET /entities/{id}` — **auth-gated** (Zitadel bearer JWT), `200` → entity incl. `prov_*`, `404`
  `{"detail":…}`, injection-shaped id → `422`.

The operator already mints Zitadel service-principal bearers (see `zitadel_provision.sh`,
`deploy/hermes/README.md`); the established, target-surface-named env vars are `WM_MCP_TOKEN` and
`WM_LLM_TOKEN`. There is **no** generic "API token" minting flow to invent — the CLI's bearer is the
same kind of Zitadel access token, held in an env var.

## Decision

Ship a **standalone, thin** CLI module `src/worldmonitor/cli.py` and a `[project.scripts] wm =
"worldmonitor.cli:main"` entry point (hatchling supports PEP 621 `[project.scripts]` natively).

1. **Zero server import-weight.** The module imports **only** `httpx` + stdlib (`argparse`, `json`,
   `os`, `sys`). It **must not** import `worldmonitor.settings`, `worldmonitor.api.*`, or any other
   package submodule. (Verified: `worldmonitor/__init__.py` is a docstring + `__version__`, so
   `import worldmonitor.cli` pulls no FastAPI/Neo4j/pydantic-settings weight.) This is the "thin
   client" the row's F-12 note implies — config is read directly from `os.environ`, not via `Settings`.

2. **Config = env + two flags; the bearer is ENV-ONLY.**
   - `WM_BASE_URL` (default `http://localhost:8000` — the API's default port), overridable by
     `--base-url`.
   - `WM_TOKEN` — the Zitadel bearer; **required for `entity`**, ignored-if-absent for `health`/`ready`.
     Named to pair with `WM_BASE_URL` and to sit alongside the existing `WM_MCP_TOKEN`/`WM_LLM_TOKEN`
     family (the `wm` CLI's bearer to our REST).
   - `WM_TIMEOUT` (default `10`s), overridable by `--timeout`.
   - **No `--token` flag** — a token on argv leaks into shell history / `ps`. Bearer via env only.

3. **Per-command behaviour** (report what the endpoint says; do **not** reinterpret):
   - `health`: `200` → body to stdout, exit 0; else exit 1 (stderr); unreachable → exit 3.
   - `ready`: prints the `/ready` body **verbatim to stdout** for both `200` and `503` (a structured
     verdict either way, `checks.driver` passed through untouched). Exit code is derived **solely from
     the HTTP status** — `200` → 0, `503` → 1 — never recomputed from `checks`; the driver field
     **never** affects the exit code (mirroring ADR 0059's non-fatal semantics). Any other status →
     exit 1 (stderr). Unreachable → exit 3.
   - `entity <id>`: pre-flight — `WM_TOKEN` unset → exit **2** (no request made); `200` → entity JSON
     to stdout **verbatim incl. every `prov_*`** (provenance never stripped); `4xx`/`5xx` → exit 1 with
     `{detail}`/`error`/`hint` to stderr; unreachable → exit 3. The **token never appears** in any
     output stream.

4. **Exit-code contract (pinned):** `0` success · `1` the API responded with a non-success outcome the
   CLI faithfully surfaces (`entity` 4xx/5xx; `ready` 503 → body to stdout) · `2` usage/config error
   (unknown command/flag — argparse default; required env missing, e.g. `WM_TOKEN` for `entity`) · `3`
   connection/timeout/transport failure (could not reach the API at all).

5. **Output:** success → `json.dumps(body, indent=2)` to **stdout** (machine-first, `jq`-friendly);
   errors → one human-readable line to **stderr**; no traceback for expected failures; no
   color/`rich`/`click`/`typer` — **no new dependency** (`httpx` is already a core dep).

6. **Transport-injectable client factory** `build_client(*, base_url, token, timeout, transport=None)`
   so unit tests drive `httpx.MockTransport` with no live server (the connectors' injection pattern).

7. **GET-only, read-only by construction** — the CLI issues only `GET`; it never writes, resolves, or
   merges. Slice 1 = the three commands and nothing else.

## Alternatives considered

- **`click`/`typer` for the CLI.** Both are installed (transitively) but neither is a declared core
  dependency; adding one to `[project.dependencies]` for three GET commands is unjustified weight.
  Stdlib `argparse` gives subcommands, `--help`, and the exit-2-on-usage-error convention for free.
  Rejected.
- **Reuse `worldmonitor.settings.Settings` for config.** Heavier (drags pydantic-settings and the
  package's env conventions) and couples the CLI to server config. The row's spirit is a thin client;
  reading `os.environ` directly is lighter and keeps the CLI importable with no server weight.
  Rejected in favour of direct env reads. *(Revisit if the CLI ever needs to share non-trivial
  validation with the server.)*
- **`ready` 503 → exit 0** (treat 503 as "successfully reported a verdict").** Rejected: a readiness
  command must be usable as a shell gate (`wm ready && deploy`), so not-ready must be non-zero. Mapping
  exit code to the **HTTP status the endpoint already chose** (200→0 / 503→1) is precisely "report what
  it says, don't reinterpret" — the CLI never re-reads `checks` to decide. *(Reversible; revisit
  trigger below.)*
- **A distinct exit code for `entity` `404` (not-found) vs other API errors.** Kept simple: 404 is a
  4xx → exit 1 with the detail surfaced. A dedicated not-found code can be added later if a consumer
  needs to branch on it (revisit trigger).
- **Accept the token via `--token`.** Rejected on security grounds (argv leaks into history / `ps`);
  env-only, and never printed.
- **A `POST`/write or an MCP-backed command.** Out of scope by the row (read-only) and by the platform
  rule (writes are gated, MCP is a separate surface). Explicitly excluded.
- **Bundle noun expansion (neighbors/paths/dossier/freshness) into this gate.** Rejected: the row says
  "noun expansion later"; keeping slice 1 to three commands keeps the gate small and the auth surface
  minimal. Recorded as slice 2.

## Consequences

- The operator/agent gets a `wm health` / `wm ready` / `wm entity <id>` client with a pinned exit-code
  contract, JSON on stdout, human errors on stderr, and an installed `wm` script — a scriptable probe
  and entity fetch over the same auth-gated REST the rest of the platform uses.
- `pyproject.toml` gains its **first** `[project.scripts]` entry; `.env.example` gains a documented CLI
  block (`WM_BASE_URL`, `WM_TOKEN`, `WM_TIMEOUT`).
- **No server code changes**, no new datastore, no migration, no write path, no MCP coupling. `httpx`
  is already a core dependency → **no new dependency**.
- **Slice 2 (later, separate gate):** `wm neighbors / paths / provenance / dossier / freshness` over
  the corresponding existing routes, sharing the `--summary` flag (F-5, already built). Server-side
  JMESPath projection is F-10; a zero-dep importable client module is F-12 — both separate gates.
- **G1 is not weakened:** the CLI displays `prov_*` verbatim and never strips provenance; it stamps
  nothing (no write path). Single-tenant (ADR 0042).

## Person-affecting reasoning

An entity fetched by `wm entity <id>` may be a `Person`, but the CLI returns the **exact bytes**
`GET /entities/{id}` already returns to an authenticated caller — no new field, no new exposure, no
provenance strip (every `prov_*` survives). It changes no ER threshold, guard mode, sensitivity park,
score, or model/param promotion; performs no inference, attribution, or resolution; writes nothing;
and its only network egress is the operator's own outbound call to the operator's own API (not a
data-sovereignty egress). Hence person_affecting = false and no cosign is required.

## Reversibility

Reversible. **Reversal cost: low** — delete `src/worldmonitor/cli.py` + the two test files, remove the
`[project.scripts]` table and the `.env.example` CLI block, regenerate the ADR index. No data written,
nothing to migrate back. **Revisit triggers:** (a) callers need noun expansion → build slice 2; (b) the
`ready` 503→exit-1 mapping conflicts with a consumer's expectation → revisit the mapping (still
"report the status", not recompute); (c) a token file / keyring is wanted over an env var → add a
credential source; (d) non-JSON or projected output is wanted → add `--compact` / defer to F-10; (e) an
OpenAPI-generated client supersedes the hand-rolled `httpx` calls (after F-7) → regenerate; (f) the CLI
needs server-shared validation → reconsider reusing `Settings`.

## Invariant gate note

F-6 touches **no** CLAUDE.md invariant that build-discipline flags for a mandatory `@given`
metamorphic/property test (no ER/merge/threshold, no canonical-id, no merge-guard/sensitivity, no
provenance **stamping**) — it is a read-only GET-only client. The load-bearing guarantees
(provenance pass-through, GET-only, token-never-leaks) are pinned as strong example tests in the spec
(§4 AC-6/AC-18/AC-10). No `@given` is mandated; recorded as a **decision, not an omission** (mirroring
ADR 0121/0122/0124).
