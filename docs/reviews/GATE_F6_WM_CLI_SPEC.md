# GATE F-6 — thin read-only `wm` CLI (slice 1: `health` / `ready` / `entity`)

> **Backlog:** `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-6** (P1 / S-M).
> **ADR:** `docs/decisions/0127-wm-cli-thin-rest-client.md` (PROPOSED → flip ACCEPTED at the
> gate-completing PR, per the 0117–0126 convention).
> **This gate = slice 1 EXACTLY:** the three commands `wm health`, `wm ready`, `wm entity <id>`,
> plus the `[project.scripts] wm` entry point, the exit-code contract, and their tests. Nothing else.

---

## 0. One-paragraph intent

A **thin, read-only** command-line client over **our own** REST API. It reads `WM_BASE_URL` +
`WM_TOKEN` from the environment, issues **GET-only** requests to three existing endpoints
(`/health`, `/ready`, `/entities/{id}`), prints the response JSON to **stdout**, human-readable
errors to **stderr**, and exits with a **pinned exit-code contract**. It is a zero-server-dependency
client: it imports **only** `httpx` + the standard library and **never** imports
`worldmonitor.settings`, `worldmonitor.api.*`, or any other package submodule (verified: the
`worldmonitor/__init__.py` chain is a docstring + `__version__` only, so `import worldmonitor.cli`
pulls no server weight). It performs **no** inference, **no** writes, **no** merges, **no** ER, and
**no** MCP coupling.

---

## 1. Verified facts the build stands on (read the code, don't re-derive)

| # | Fact | Source |
|---|------|--------|
| V1 | `GET /health` is **public** (in `DEFAULT_PUBLIC_PATHS`), always `200`, body `{"status":"ok","environment":<str>}`. | `api/middleware.py:36`, `api/main.py:172` |
| V2 | `GET /ready` is **public**, body `{"ready":<bool>,"checks":{...}}`, HTTP **`200` iff `ready`, else `503`** (fail-closed, stores-only). | `api/main.py:177`, `api/readiness.py:55` |
| V3 | `/ready`'s `checks.driver` is **observability-only** — `"ok"`/`"stale"`/`"unknown"`, **non-fatal**, never flips `ready` or the HTTP status (ADR 0059). | `api/readiness.py:84–90,152–171` |
| V4 | `GET /entities/{id}` is **auth-gated** (`Depends(get_principal)`); `200` → entity dict incl. `prov_*`; `404` `{"detail":"Entity not found"}`; injection-shaped id → `422`. (Path-traversal-shaped ids, e.g. `../ready`, never reach this API-side check at all — the CLI percent-encodes the id segment, so `../ready` is neutralized client-side to a bogus path under `/entities/` → `404`/exit 1, not `422`.) | `api/graph.py:46` |
| V5 | Auth is a **Zitadel bearer JWT** in `Authorization: Bearer <jwt>`; a tokenless/invalid API call gets the frozen `401 {"detail":"..."}` (never a redirect for JSON callers). | `api/middleware.py:106–126` |
| V6 | The API listens on **port 8000** by default. | `Dockerfile:77`, `deploy/compose.yaml:300,355` |
| V7 | The operator already mints service-principal bearers via Zitadel; existing target-surface-named env vars are **`WM_MCP_TOKEN`** (Hermes→MCP) and **`WM_LLM_TOKEN`** (Hermes→LLM). | `deploy/hermes/README.md:50,83`, `scripts/dev/zitadel_provision.sh` |
| V8 | `httpx==0.28.1` with `httpx.MockTransport` is installed → transport injection is available for unit tests with no live server. | `.venv` verified |
| V9 | `pyproject.toml` has **no** `[project.scripts]` today; build backend is **hatchling** (PEP 621 `[project.scripts]` supported natively). | `pyproject.toml:41–46` |

---

## 2. Scope — exact files

**New:**
- `src/worldmonitor/cli.py` — the whole CLI: `build_client()`, the three command handlers, `main()`.
- `tests/unit/test_cli.py` — unit tests via injected `httpx.MockTransport`.
- `tests/unit/test_cli_entrypoint.py` — entry-point smoke (`python -m worldmonitor.cli --help`) +
  the `[project.scripts]` assertion.

**Modified:**
- `pyproject.toml` — add the `[project.scripts]` table (one line: `wm = "worldmonitor.cli:main"`).
- `.env.example` — document `WM_BASE_URL`, `WM_TOKEN`, `WM_TIMEOUT` (a new commented CLI block).
- `docs/decisions/0127-wm-cli-thin-rest-client.md` — flip PROPOSED → ACCEPTED at gate close.
- `docs/decisions/README.md` — regenerated ADR index (`python scripts/gen_adr_index.py`).

**Out of scope (hard non-goals — do NOT touch):** any `src/worldmonitor/api/*`,
`src/worldmonitor/mcp/*`, `src/worldmonitor/graph/*`, `settings.py`, any connector, any write path.
The CLI calls the REST surface **as an external client**; it changes no server code.

---

## 3. Behaviour contract (pin exactly)

### 3.1 Configuration (env + flags; token is ENV-ONLY)

| Name | Kind | Default | Notes |
|------|------|---------|-------|
| `WM_BASE_URL` | env | `http://localhost:8000` | Base URL of our REST API. Override for remote/https. |
| `--base-url URL` | flag | (env) | Overrides `WM_BASE_URL` for one invocation. |
| `WM_TOKEN` | env | (unset) | The Zitadel bearer. **Required for `entity`**, ignored-if-absent for `health`/`ready`. |
| `WM_TIMEOUT` | env | `10` | Per-request timeout, seconds (float). |
| `--timeout SECONDS` | flag | (env) | Overrides `WM_TIMEOUT`. |

- **There is NO `--token` flag.** The bearer is accepted **only** via `WM_TOKEN` (never on argv — it
  would leak into shell history / `ps`). Passing `--token` is an unknown flag → exit **2**.
- The client attaches `Authorization: Bearer <WM_TOKEN>` **iff** `WM_TOKEN` is set, uniformly for all
  requests (public endpoints ignore it; harmless — it is the operator's own token to their own API).

### 3.2 Per-command behaviour

**`wm health`** → `GET {base}/health`
- `200` → print body JSON to **stdout**, exit **0**.
- non-`200` → exit **1**, `{detail}` (or raw body) to **stderr**.
- unreachable/timeout → exit **3**, human message to stderr.
- Requires **no** token.

**`wm ready`** → `GET {base}/ready` — **report what the endpoint says, do NOT reinterpret** (ADR 0059)
- The command prints the `/ready` body **verbatim to stdout** for **both** `200` and `503` (it is a
  structured readiness verdict either way, including `checks.driver` passed through untouched).
- Exit code is derived **solely from the HTTP status**: `200` → **0**, `503` → **1**. The CLI does
  **not** recompute readiness from `checks`, and `checks.driver` (`ok`/`stale`/`unknown`) **never**
  affects the exit code — mirroring the endpoint, where the driver field is non-fatal.
- Any status that is neither `200` nor `503` (e.g. a bare `500` with no ready-envelope) → generic
  API-error path: exit **1**, message to **stderr**.
- unreachable/timeout → exit **3**. Requires **no** token.

**`wm entity <id>`** → `GET {base}/entities/{id}`
- **Pre-flight:** if `WM_TOKEN` is unset → exit **2**, stderr `"WM_TOKEN is required for 'entity'"`,
  **no request is made**.
- `200` → print the entity JSON to **stdout verbatim** (incl. every `prov_*` field — provenance is
  **never** stripped), exit **0**.
- `4xx`/`5xx` (401/403/404/422/5xx) → exit **1**, surface the API's `{detail}` (and `error`/`hint`
  if present) to **stderr**. The bearer token **never** appears in any output stream.
- unreachable/timeout → exit **3**.

### 3.3 Exit-code contract (the row's explicit ask — pinned)

| Code | Meaning |
|------|---------|
| **0** | Success. `health`/`entity` = HTTP `200`; `ready` = HTTP `200` (ready). |
| **1** | The API **responded** with a non-success outcome the CLI faithfully surfaces: `entity` `4xx`/`5xx`; `ready` `503` (not-ready verdict → body to **stdout**); any other non-`200` on `health`/`ready` (message to **stderr**). |
| **2** | **Usage / configuration** error: unknown subcommand, unknown/bad flag (argparse default), or a required env var missing for the requested command (`WM_TOKEN` unset for `entity`; `--base-url ""` / empty base). No request made. |
| **3** | **Connection/transport** failure — could not reach the API at all (`httpx.ConnectError`, `httpx.TimeoutException`, DNS/TLS error). Distinct from an API that responded with an error (1). |

### 3.4 Output contract

- **Success → stdout:** `json.dumps(body, indent=2)` + trailing newline (machine-first, `jq`-friendly).
  Provenance (`prov_*`) rides through verbatim.
- **Error → stderr:** a single human-readable line; for API errors, the surfaced `detail`/`error`/`hint`.
  Never a Python traceback for an expected failure (1/2/3). No color, no `rich`/`click`/`typer` — stdlib
  `json` only.

### 3.5 Entry point

- `pyproject.toml` gains: `[project.scripts]` → `wm = "worldmonitor.cli:main"`.
- `main(argv: list[str] | None = None) -> int` returns the exit code (the console-script wrapper does
  `sys.exit(main())`). `cli.py` ends with `if __name__ == "__main__": sys.exit(main())` so
  `python -m worldmonitor.cli` works identically.

### 3.6 Transport-injection design (for tests)

- `build_client(*, base_url: str, token: str | None, timeout: float, transport: httpx.BaseTransport | None = None) -> httpx.Client`
  builds the `httpx.Client` (base_url, headers, timeout); when `transport` is provided it is passed to
  `httpx.Client(transport=...)`. Tests inject `httpx.MockTransport(handler)`; no live server, no network.

---

## 4. Acceptance criteria → named tests (each AC has a test that proves it)

`tests/unit/test_cli.py` (unit, via `httpx.MockTransport`):

| AC | Criterion | Named test |
|----|-----------|-----------|
| AC-1 | `health` `200` → exit 0, body on stdout | `test_health_success_exit0` |
| AC-2 | `health` works with `WM_TOKEN` unset (public) | `test_health_no_token_required` |
| AC-3 | `ready` `200` → exit 0, body (incl. `checks`) on stdout | `test_ready_ready_exit0` |
| AC-4 | `ready` `503` → exit **1**, body on **stdout** verbatim (not stderr) | `test_ready_notready_503_exit1_body_on_stdout` |
| AC-5 | `ready` `200` with `checks.driver == "stale"` → still exit **0** (driver non-fatal, not reinterpreted); driver field passed through | `test_ready_driver_stale_still_exit0` |
| AC-6 | `entity` `200` → exit 0; **every `prov_*` survives** on stdout (provenance not stripped) | `test_entity_success_preserves_provenance_exit0` |
| AC-7 | `entity` sends `Authorization: Bearer <token>` (captured in the handler) | `test_entity_sends_bearer_header` |
| AC-8 | `entity` with `WM_TOKEN` unset → exit **2**, stderr message, **no request made** | `test_entity_missing_token_exit2_no_request` |
| AC-9 | `entity` `404` → exit **1**, `detail` on stderr | `test_entity_404_exit1_detail_on_stderr` |
| AC-10 | `entity` `401` with a set token → exit **1**; **the token string appears in NEITHER stdout NOR stderr** | `test_entity_401_never_leaks_token` |
| AC-11 | connection failure → exit **3**, no traceback | `test_connection_error_exit3` |
| AC-12 | timeout → exit **3** | `test_timeout_exit3` |
| AC-13 | unknown subcommand → exit **2** | `test_unknown_command_exit2` |
| AC-14 | no subcommand → exit **2** (usage) | `test_no_subcommand_exit2` |
| AC-15 | `--base-url` overrides the env (captured request host matches) | `test_base_url_flag_overrides_env` |
| AC-16 | `--timeout` is parsed onto the client | `test_timeout_flag_applied` |
| AC-17 | **no `--token` flag exists** (`--token x` → exit 2 unknown flag) | `test_token_flag_rejected_env_only` |
| AC-18 | GET-only: no command ever issues a non-GET verb (handler asserts `request.method == "GET"`) | `test_all_commands_are_get_only` |

`tests/unit/test_cli_entrypoint.py` (entry-point smoke):

| AC | Criterion | Named test |
|----|-----------|-----------|
| AC-19 | `python -m worldmonitor.cli --help` exits 0 and prints usage listing the three subcommands | `test_module_help_runs` (subprocess with `PYTHONPATH=src`) |
| AC-20 | `pyproject.toml` declares `[project.scripts] wm = "worldmonitor.cli:main"` | `test_project_script_entry_declared` (parse pyproject, no install needed) |

**Green bar:** all of `tests/unit/test_cli.py` + `tests/unit/test_cli_entrypoint.py` pass under
`uv run pytest -m "not integration" tests/unit/test_cli.py tests/unit/test_cli_entrypoint.py`; full
`pytest -m "not integration"`, `ruff check`, `ruff format --check .`, and `pyright` (strict on `src`)
stay green; `python scripts/gen_adr_index.py --check` passes (index regenerated at gate close).

---

## 5. Locked invariants this gate must hold

The CLI is a **read-only external client**; it upholds the CLAUDE.md invariants by **non-violation**,
proven by test, not merely asserted:

- **G1 (provenance on every node AND edge):** the CLI performs **no** write and **no** stamping. It
  prints entity bodies **verbatim including `prov_*`** and must **never** strip provenance
  (AC-6). Untouched — read-only display of what the API already returns.
- **Append-only / read-only by construction:** the CLI issues **GET only** — never
  POST/PUT/PATCH/DELETE, never a graph write (AC-18 asserts every command's method is `GET`). No ER,
  no merge, no resolution, no scoring.
- **Canonical-canonical merge only via the guard:** **N/A** — the CLI never merges or resolves.
  Untouched.
- **Provenance stamping is not toggleable:** untouched (no write path exists in the CLI).
- **Secret hygiene:** the bearer is **env-only** (no `--token` flag, AC-17) and **never** printed to
  any stream (AC-10). No secret is hardcoded.

**`@given` property test — not mandated, recorded as a decision (per the ADR 0121/0122/0124
convention).** This gate touches **no** invariant that build-discipline flags for a mandatory
metamorphic/property test (no ER/merge/threshold, no canonical-id, no merge-guard/sensitivity, no
provenance **stamping**). The load-bearing guarantees here — provenance pass-through (AC-6), GET-only
(AC-18), token-never-leaks (AC-10) — are pinned as strong example tests. No `@given` is required;
this is a decision, not an omission.

---

## 6. Slice breakdown (individually mergeable, each with its own green tests)

Two slices. **Slice B stacks on Slice A** (extends the same module); each is a complete, CI-green,
individually-mergeable PR. (May ship as one PR if the fleet prefers; the split isolates the
security-sensitive auth path into its own review.)

### Slice A — CLI skeleton + the two public commands (`health`, `ready`) + entry point
- `src/worldmonitor/cli.py`: `build_client()` (transport-injectable), argparse with `health`/`ready`
  subcommands + `--base-url`/`--timeout`, the exit-code dispatcher, `main()`, `__main__` guard.
- `pyproject.toml`: `[project.scripts] wm = "worldmonitor.cli:main"`.
- `.env.example`: `WM_BASE_URL` + `WM_TIMEOUT` block.
- Tests: AC-1..5, AC-11..16, AC-18, AC-19, AC-20.
- **Mergeable alone:** a working `wm health` / `wm ready` CLI with the full exit-code + output
  contract and the installed entry point. No auth surface yet.

### Slice B — `wm entity <id>` (the auth-gated command)
- `src/worldmonitor/cli.py`: add the `entity` subcommand, `WM_TOKEN` handling, the bearer header, the
  missing-token pre-flight (exit 2), and the auth-failure UX (401/403 → exit 1, no token leak).
- `.env.example`: add the `WM_TOKEN` line to the CLI block.
- Tests: AC-6..10, AC-17.
- **Mergeable after A:** adds exactly one subcommand; leaves `health`/`ready` byte-identical.

---

## 7. Non-goals — recorded so they are not smuggled in

Per the task: **no** noun expansion, **no** write op, **no** MCP coupling, **no** shell completions,
**no** packaging/distribution beyond the `[project.scripts]` entry.

**Natural slice-2 list (record, do not build now):**
- `wm neighbors <id> [--hops N] [--summary]` → `GET /entities/{id}/neighbors`
- `wm paths --from <id> --to <id> [--max-hops N] [--summary]` → `GET /paths`
- `wm provenance <id>` → `GET /entities/{id}/provenance`
- `wm dossier <id> [--hops N]` → `GET /entities/{id}/dossier`
- `wm freshness` → `GET /sources/freshness`
- shared `--summary` pass-through (F-5 is already built); server-side JMESPath projection is **F-10**
  (separate gate); a zero-dep importable client module is **F-12** (separate gate).
