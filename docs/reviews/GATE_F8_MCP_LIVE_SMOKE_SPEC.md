# Gate F-8 — MCP live-smoke in CI (compose-boot rider)

> Backlog: `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-8** — "MCP live-smoke in CI —
> compose-boot step: authenticated `tools/list` asserts exactly the registered tool set. XS rider on
> compose-boot. **P2 / XS**."
> ADR: `docs/decisions/0126-mcp-live-smoke.md` (PROPOSED → ACCEPTED at the gate-completing PR, per the
> 0117–0125 convention).
> Predecessors relied on: ADR 0034 (compose-boot exists to catch deploy-config defects testcontainers
> miss), 0051 (compose-boot builds the app image + waits api/driver healthchecks), 0063 (stdio FastMCP;
> trust boundary = who may spawn the process, **no per-call token** — single-tenant D1/ADR 0042), 0088
> (`alert-rules` promtool-in-CI precedent for a CI check with a checked-in artifact + local testability),
> 0090 (authenticated HTTP MCP transport), 0122 (5th tool `get_entity_dossier`; F-3 pin-sweep lesson),
> 0125 / Gate F-4 (the two prompts `entity-workup` + `freshness-audit`).

> **F-8 ordering (load-bearing):** this gate's fleet starts **after** Gate F-4 (ADR 0125) has merged to
> `master`. The chosen assertion sets (§3) are the **post-F-4** surface: exactly **5 tools + 2 prompts**.
> If F-4 is somehow not on `master` when F-8 builds, drop the `prompts/list` assertion and the
> `EXPECTED_PROMPTS` constant (see §12 open item 1) — the tool-set assertion is F-4-independent. The
> coordinator moves this spec + `docs/decisions/0126-mcp-live-smoke.md` into the repo and applies
> `.claude/gate.scope` (§gate.scope, at the very end) at fleet start.

---

## 0. What this gate is (and is NOT)

**Is:** a single **additive** step appended to the existing `compose-boot` CI job that runs a **live MCP
smoke against the real deployed image** (`worldmonitor-app:dev`, the same image the `mcp` service runs) and
**fails the job** if the served surface is not **exactly** the registered set of **5 tools + 2 prompts**.
The smoke speaks real JSON-RPC over the **stdio** transport to a freshly-spawned server process (the
deployed `python -m worldmonitor.mcp` entrypoint), so it needs **no bearer, no Zitadel, no Neo4j** — it
works with what compose-boot actually boots (§1). The assertion is a **drift pin**: any 6th tool, any 3rd
prompt, any renamed/dropped member, or any import failure in the deployed image turns the required check red.

**Is NOT** (explicit non-goals — see §9):
- **No new runtime behaviour, no new service, no new profile, no code in `src/worldmonitor/mcp/server.py`.**
  The MCP tool/prompt surface is unchanged. This gate only adds a CI check + a tiny package smoke module.
- **No HTTP / authenticated-transport assertion in compose-boot.** compose-boot boots **no Zitadel and no
  `mcp` HTTP service**; a real OIDC bearer cannot be minted without booting **and** provisioning Zitadel
  (`zitadel_provision.sh`) — far beyond an XS rider (§1.3). The "authenticated" qualifier in the backlog row
  is honestly degraded to a **stdio (no-auth-transport) live smoke** here; the HTTP **401/403 auth boundary
  stays pinned by `tests/integration/test_mcp_http_transport.py`** (unchanged). Revisit trigger recorded
  (§1.3, §9, ADR 0126 A2).
- **No change to any existing compose-boot step.** The change is one **appended** `run:` step; the store /
  api / driver / sandbox-runner boots and their timeouts are untouched (§5.3 — required-check discipline).
- **No `mcp`/FastMCP version bump; no tool-call, no Neo4j write/read; no new dependency** (stdlib + the
  already-installed `mcp` package only).

---

## 1. Honest auth / boot investigation — what compose-boot ACTUALLY boots

### 1.1 The job today (`.github/workflows/compose-boot.yml`, required check)

`compose-boot` is a **branch-protection-required** check (one of the 7: quality / security / integration /
alert-rules / adr-index / ftm-schema / **compose-boot**). Its steps, in order:

1. Write a `.env` for the required compose vars (`NEO4J_PASSWORD=ci-neo4j-password`, a valid Fernet
   `CONFIG_ENCRYPTION_KEY`, everything else `ci-unused`).
2. `docker compose config` (interpolation validates).
3. **Build** `api driver sandbox-runner` (all share `image: worldmonitor-app:dev`, `target: runtime`; the
   sandbox uses `target: sandbox-runner`).
4. `up -d --wait` **`postgres neo4j minio`**.
5. `up -d --wait` **`api driver`** (pulls in `migrate` + `minio-init` one-shots via depends_on).
6. `up -d --wait` **`sandbox-runner`**.
7. Status + on-failure logs + `down -v`.

**It never boots `zitadel`, never boots the `mcp` service, never boots `hermes`.** The smoke it does today
is healthcheck-based (`--wait`), not a bespoke client.

### 1.2 The `mcp` service (deploy/compose.yaml)

```
mcp:
  image: worldmonitor-app:dev
  command: ["python", "-m", "worldmonitor.mcp"]
  profiles: ["agent"]                       # NOT started by a bare `up`
  depends_on: { neo4j: service_healthy, zitadel: service_healthy }
  environment:
    MCP_TRANSPORT: streamable-http           # → the HTTP (bearer-gated) path
    ZITADEL_DOMAIN: ${ZITADEL_DOMAIN:-}      # empty in compose-boot's .env
    NEO4J_PASSWORD: ${NEO4J_PASSWORD}        # set (ci-neo4j-password)
```

So the deployed `mcp` service, as configured, runs the **HTTP** transport and `main()` **hard-raises at
boot without `ZITADEL_DOMAIN`** (server.py:588-593):
> `mcp_transport=streamable-http requires zitadel_domain … cannot verify tokens without an OIDC issuer/JWKS.`

### 1.3 Why "authenticated HTTP `tools/list`" is impossible here (honest)

To assert an **authenticated HTTP** `tools/list` in compose-boot we would need, in ascending cost:
(i) boot `zitadel` (a new required-check dependency + a healthcheck wait), (ii) run `zitadel_provision.sh` to
mint a machine identity + client, (iii) obtain a real signed bearer with the `worldmonitor:read` scope,
(iv) set `ZITADEL_DOMAIN`/`ZITADEL_CLIENT_ID`/`MCP_RESOURCE_SERVER_URL` and boot the `mcp` HTTP service.
That is a multi-service auth bring-up — **not XS**, and it duplicates coverage the HTTP integration suite
already owns (`tests/integration/test_mcp_http_transport.py` pins **401 no-bearer / 403 no-role / 200 with
role** on the real `streamable_http_app()` via an in-proc fake verifier).

**The evaluated menu (task §1):**
- **(a) stdio smoke against the deployed image — CHOSEN.** stdio needs **no bearer** (ADR 0063: the trust
  boundary is who may spawn the process, single-tenant D1). It runs the **real deployed entrypoint** and
  asserts the exact set over a **live transport**. Least invasive, honest, genuinely "live".
- (b) a compose-boot-only test-auth profile — **rejected**: booting+provisioning Zitadel is far too invasive
  for an XS rider (§1.3 above).
- (c) HTTP endpoint → expect 401 (auth-works) **plus** a stdio set-assertion — **rejected for compose-boot**:
  the 401 half still requires the `mcp` HTTP service, which `depends_on zitadel service_healthy` **and**
  hard-raises without `ZITADEL_DOMAIN`. We do **not** relax the service's depends_on or its fail-closed
  boot guard (both are correct production posture). The 401/403 boundary is already covered by the HTTP
  integration suite; re-proving it in compose-boot buys nothing for the cost.

### 1.4 Why the stdio smoke is store- and auth-free (verified in code)

- `python -m worldmonitor.mcp` → `main()`; with `MCP_TRANSPORT` set to `stdio` (the smoke forces this on the
  child, §2) it takes the **else** branch: `build_server().run(transport="stdio")` — **no Zitadel check, no
  verifier, no port** (server.py:604-605).
- `build_server()` with no injected client calls `Neo4jClient.from_settings()` →
  `GraphDatabase.driver(uri, auth=…)`, which is **lazy** (`verify()`/`verify_connectivity` is a *separate*
  method that `build_server` never calls — neo4j_client.py:34,46-48). So construction opens **no
  connection**.
- `tools/list` and `prompts/list` **never call a tool** → `execute_read` is never reached → **Neo4j is
  never contacted**. The smoke needs only that `NEO4J_*` env vars *exist* (they do: the `mcp` service sets
  them, and `NEO4J_PASSWORD` is in compose-boot's `.env`). No stores need to be up for the smoke to pass.
  (They will be up anyway if the step is placed after the store boot — §5.2 — so the design is robust even
  if `from_settings` were ever made eager.)

**Conclusion:** the chosen design (a) is the only one that is simultaneously honest, "live", and XS.

---

## 2. Chosen design — the stdio live smoke

A new **package module** `src/worldmonitor/mcp/smoke.py`, invoked as **`python -m worldmonitor.mcp.smoke`**
(mirrors the existing `python -m worldmonitor.runner.smoke_metrics` smoke-entrypoint precedent). It:

1. **Spawns the real deployed entrypoint** `[sys.executable, "-m", "worldmonitor.mcp"]` as a subprocess with
   `env = os.environ | {"MCP_TRANSPORT": "stdio"}` — forcing the **stdio** path regardless of the service's
   `MCP_TRANSPORT=streamable-http`. `stdin=PIPE, stdout=PIPE, stderr=PIPE`, `cwd=/app`.
2. **Speaks minimal JSON-RPC** over the child's stdin/stdout (newline-delimited JSON frames — the same wire
   idiom `tests/integration/test_mcp_stdio.py` already uses): `initialize` (id 1, `protocolVersion =
   mcp.types.LATEST_PROTOCOL_VERSION` from the **same installed `mcp` package**, so client/server versions
   can never mismatch) → `notifications/initialized` → `tools/list` (id 2) → `prompts/list` (id 3).
3. **Collects** `served_tools = {t["name"] …}` and `served_prompts = {p["name"] …}` from the two result
   frames.
4. **Asserts** `served_tools == EXPECTED_TOOLS` **and** `served_prompts == EXPECTED_PROMPTS` (hardcoded
   frozensets, §3). On any mismatch it prints the **symmetric difference** (`missing=…, unexpected=…`) for
   each set to **stderr** and **exits non-zero**. On exact match it prints a one-line OK summary and exits 0.
5. **Bounds itself:** a per-read deadline (≈60 s, generous for the PyICU/followthemoney import cost) and a
   final `child.terminate()` in a `finally`; a handshake/timeout failure **exits non-zero** and dumps the
   child's captured stderr (matching the test's "attach the spawned process's stderr" idiom). It never hangs
   the 25-minute job.

**Why a package module and NOT `scripts/…` or inline YAML (load-bearing):** the `Dockerfile` copies **only**
`pyproject.toml`, `uv.lock`, `README.md`, and `src/` into the runtime image — **`scripts/` is NOT in the
image**. A `scripts/smoke/*.py` would be absent inside `worldmonitor-app:dev` and could not be run by
`docker compose run mcp …`. A package module under `src/worldmonitor/mcp/` is installed into the image (the
`worldmonitor.runner.smoke_metrics` precedent) and is directly unit-testable locally (§7). Inline Python in
YAML is rejected: not locally testable, not reusable, ugly to maintain a set-pin in.

**Why the real wire (not an in-process `build_server().list_tools()`):** the in-process form duplicates
`test_mcp_server.py::test_tool_set_is_exactly_the_five` and proves nothing new about the *deployed artifact*.
The subprocess/wire smoke exercises the **actual deployed entrypoint** (`python -m worldmonitor.mcp` →
`.run(transport="stdio")` → the JSON-RPC handlers) inside the **built image**, catching a distinct class:
"the image built, the package imports, the transport actually serves the exact registered set." That is what
`compose-boot` exists to catch (ADR 0034/0051) and is the honest reading of "live-smoke". The in-process
enumeration is recorded as the rejected simpler alternative (ADR 0126 A1).

### 2.1 The compose-boot step (one appended `run:` step)

```yaml
- name: MCP live-smoke — stdio tools/list + prompts/list assert the exact registered surface
  # Runs the deployed image's own MCP entrypoint under the stdio transport (no bearer needed —
  # ADR 0063 single-tenant) and asserts EXACTLY the 5 tools + 2 prompts. --no-deps because the
  # smoke opens no Neo4j connection (tools/list/prompts/list call no tool); --profile agent so the
  # profiled `mcp` service resolves; -T because CI has no TTY; --rm to clean up the one-shot.
  run: |
    docker compose --env-file .env -f deploy/compose.yaml --profile agent \
      run --rm --no-deps -T mcp python -m worldmonitor.mcp.smoke
```

- `run --rm mcp COMMAND` **overrides** the service `command` with `python -m worldmonitor.mcp.smoke`; the
  smoke module then spawns the stdio child itself.
- `--no-deps` is **required** so compose does not try to start the `mcp` service's `depends_on`
  (`zitadel`, `neo4j`) — we neither need nor want Zitadel.
- `--profile agent` ensures the profiled `mcp` service is resolvable to `run` (builder verifies; a bare
  `run` targeting the service by name is usually enough, add the flag if compose complains).
- Exit non-zero from the smoke → the step fails → the required check is red (§4).

---

## 3. The assertion sets (exact — the pins)

Hardcoded in `smoke.py` as frozensets; **also** guarded against the live registration by a local unit test
(§7, the lockstep guard).

```python
EXPECTED_TOOLS = frozenset({
    "get_entity", "get_neighbors", "get_provenance", "find_paths", "get_entity_dossier",
})                                   # the 5 read tools (ADR 0063 + 0122)

EXPECTED_PROMPTS = frozenset({
    "entity-workup", "freshness-audit",
})                                   # the 2 analyst-playbook prompts (ADR 0125 / Gate F-4)
```

**Decision — include `prompts/list` in the smoke (record):** the backlog row predates prompts (F-4). Adding
the prompt-set assertion is a natural, cheap extension (`prompts/list` is one more JSON-RPC round-trip on the
already-spawned server) and it closes the same drift class for the prompt surface that F-4 introduced.
**Included.** (If F-4 is not yet on `master` at build time, drop it — §12 open item 1.)

**Set equality, both directions.** The assertion is `==` (exact set), so it fails on **extra** members
(an accidentally-registered 6th tool / raw-Cypher tool) as loudly as on **missing** members. This is the
INV of the pin: the surface is *exactly* these, not *at least* these.

---

## 4. Failure semantics

- **Drift → hard FAIL, never warn.** Any set mismatch → the smoke exits non-zero → the `run:` step fails →
  `compose-boot` is red. No `continue-on-error`, no soft warning. The stderr output names
  `tools: missing={…} unexpected={…}` / `prompts: missing={…} unexpected={…}` for a one-glance diagnosis.
- **Handshake / spawn / timeout → FAIL.** If `initialize` errors, the child dies, or a read exceeds the
  deadline, the smoke exits non-zero and dumps the child's captured **stderr** (the server logs to stderr;
  a crash/traceback lands there, never on the JSON-RPC stdout channel — STDOUT PURITY, ADR 0063).
- **Timeout budget consistent with the job.** The smoke's internal read deadline (≈60 s) sits well under the
  job's `timeout-minutes: 25` and the store boots' `--wait-timeout 300`; it is a bounded, self-terminating
  step in the same spirit as the existing `--wait`-bounded steps. No retry loop (a set assertion is
  deterministic; a flake would be a real defect, not noise — do not paper over it with retries).
- **`down -v` (always) still runs.** The appended step sits before the existing `Status` / on-failure-logs /
  `Tear down (if: always())` steps, so teardown is unaffected on either outcome.

---

## 5. Placement, scope, and required-check discipline

### 5.1 Files (exact)

| Path | Change | Why |
|---|---|---|
| `src/worldmonitor/mcp/smoke.py` | **new package module** | The stdio live-smoke: `EXPECTED_TOOLS`/`EXPECTED_PROMPTS`, `run_smoke()` (spawn+handshake+assert), `main()` (exit code). In the image because it is under `src/` (Dockerfile copies `src/`, **not** `scripts/`). |
| `.github/workflows/compose-boot.yml` | **edit (append ONE step only)** | The `run:` step of §2.1. **No existing step is modified, removed, or reordered.** |
| `tests/unit/test_mcp_smoke.py` | **new** | The lockstep guard (§7): the hardcoded expected sets == the live `build_server()` registration; plus the diff/exit-code unit behaviour. |
| `docs/decisions/0126-mcp-live-smoke.md` | **new** | This gate's ADR (PROPOSED → ACCEPTED at the merging PR). |
| `docs/decisions/README.md` | **regenerate** | `python scripts/gen_adr_index.py` — the `adr-index` required check scans `docs/decisions/(\d{4})-*.md`; the new ADR must be indexed or that check fails. |
| `docs/reviews/GATE_F8_MCP_LIVE_SMOKE_SPEC.md` | **new** | This spec. |

**Out of scope / must NOT change (stay green as regression pins):** `src/worldmonitor/mcp/server.py`,
`src/worldmonitor/mcp/auth.py`, `deploy/compose.yaml` (the `mcp`/`zitadel` services are untouched),
`tests/unit/test_mcp_server.py::test_tool_set_is_exactly_the_five`, the F-4 prompt-set test, and every
existing compose-boot step.

### 5.2 Where the step slots

Append the new step **after** `Boot sandbox-runner …` and **before** `Status` (purely additive; the smoke is
store-independent per §1.4, but placing it after the store/api/driver boots is harmless and keeps the append
at the end of the boot sequence). Do **not** insert it between existing boot steps (avoid any perception of
reordering).

### 5.3 Branch-protection note (state explicitly — required)

`compose-boot` is a **branch-protection-required** check. Editing the job **changes the merge gate for the
whole repo**. Therefore the change is strictly **additive**: exactly one **appended** `run:` step; **no
existing step is weakened, removed, reordered, or made `continue-on-error`**; the job's `timeout-minutes`,
triggers (`on:`), and store/api/driver/sandbox waits are unchanged. The new step can only ever make the gate
**stricter** (it can fail on drift), never looser. If the smoke itself is flaky the correct fix is the smoke,
never relaxing an existing step.

---

## 6. Locked invariants

- **G1 provenance — N/A (honestly).** The smoke reads no graph nodes/edges (`tools/list`/`prompts/list`
  return the *surface*, not data); there is no provenance to carry or strip. The five tools the smoke
  *enumerates* still return `prov_*` unchanged (this gate does not touch them).
- **Append-only / read-only.** The smoke opens no Neo4j session and issues no Cypher (§1.4). It writes
  nothing to any store. It builds no client that connects.
- **Canonical-canonical via the guard — N/A.** No merge, no ER, no write path is touched.
- **STDOUT PURITY (inherited, re-pinned by construction).** The smoke relies on the server's stdout being
  **only** JSON-RPC frames; a stray stdout print would corrupt the handshake and **fail** the smoke — so the
  smoke is itself an incidental live guard of STDOUT PURITY over the deployed transport. (The dedicated
  property test `test_prop_mcp_stdout_purity.py` remains the primary pin.)

This gate touches **no CLAUDE.md invariant** — it is CI wiring around a read-only surface. No enforcement
switch, no person-affecting path, no egress, no write.

---

## 7. Property-test discipline + local testability

**No mandatory `@given`.** F-8 touches no CLAUDE.md invariant (no provenance-stamping, no ER/merge/threshold,
no canonical-id resolution, no guard, no write, no egress) — the build-discipline mandatory-property rule
does not apply. **This is a decision, recorded in ADR 0126, not an omission.** The **compose-boot job itself
is the test** (the `alert-rules`/`adr-index` precedent: a CI check whose artifact is also locally testable).

**Local unit test (cheap, high-value — keep XS):** because the smoke lives in the package, its **pins are
locally testable**. `tests/unit/test_mcp_smoke.py`:

1. **Lockstep guard (the meaningful one):** assert `smoke.EXPECTED_TOOLS == {t.name for t in
   asyncio.run(build_server().list_tools())}` **and** `smoke.EXPECTED_PROMPTS == {p.name for p in
   asyncio.run(build_server().list_prompts())}`. This ties the smoke's hardcoded pins to the **real live
   registration** so the pins cannot silently rot: a legitimate future 6th tool / 3rd prompt fails this test
   until the smoke constants are updated too (the future-sweep coupling — §10). Pure in-process, no
   subprocess, fast, non-flaky.
2. **Diff/exit behaviour:** a small helper test that `run_smoke()`'s compare-and-report logic returns
   non-zero + a symmetric-diff message when handed a surface that differs from the expected sets, and zero
   when it matches (drive the compare function with injected name-sets — no subprocess needed).
3. **(Optional, `@pytest.mark.integration`, only if non-flaky):** spawn the local `python -m
   worldmonitor.mcp` and run the full `run_smoke()` end-to-end, asserting exit 0 — the same subprocess idiom
   as `test_mcp_stdio.py`. Include only if it does not add flake; the two unit tests above already guard the
   pins.

The **test-author's role here is minimal** (CI-wiring gate): author (1) and (2) above; (3) is optional.
There is no RED-invariant to prove — the "reality check" is the live compose-boot run plus the lockstep unit
test.

---

## 8. Named tests (the proof this gate holds)

- `tests/unit/test_mcp_smoke.py::test_expected_tool_set_matches_live_registration` — lockstep (§7.1).
- `tests/unit/test_mcp_smoke.py::test_expected_prompt_set_matches_live_registration` — lockstep (§7.1).
- `tests/unit/test_mcp_smoke.py::test_smoke_compare_flags_drift_nonzero` — extra/missing member → non-zero +
  symmetric-diff message (§7.2).
- `tests/unit/test_mcp_smoke.py::test_smoke_compare_exact_match_zero` — exact set → zero (§7.2).
- *(optional)* `tests/unit/test_mcp_smoke.py::test_smoke_end_to_end_stdio_exit_zero`
  (`@pytest.mark.integration`) — real subprocess handshake → exit 0 (§7.3).
- **The compose-boot job itself** (`.github/workflows/compose-boot.yml`, new step) — the live smoke against
  the built image; **red on any surface drift**. This is the gate's headline "test".

**Regression (unchanged, must stay green):** `test_mcp_server.py::test_tool_set_is_exactly_the_five`, the
F-4 prompt-set + stdio/http prompt tests, both MCP property tests, and every existing compose-boot step.

---

## 9. NON-goals (explicit)

- **An authenticated HTTP `tools/list` in compose-boot** — structurally impossible without booting +
  provisioning Zitadel (§1.3); the 401/403/200 auth boundary stays pinned by
  `test_mcp_http_transport.py`. Out (revisit trigger only — ADR 0126 A2).
- **Booting `zitadel` / the `mcp` HTTP service / a test-auth profile in compose-boot.** Out (too invasive
  for XS; §1.3).
- **Modifying, reordering, or weakening any existing compose-boot step**, or changing the `mcp`/`zitadel`
  compose service definitions. Out (§5.3, required-check discipline).
- **A tool-call / Neo4j read in the smoke** (e.g. `get_entity`) — the smoke asserts the *surface*, not data;
  calling a tool would need seeded data + a live Neo4j and is a different (heavier) check. Out.
- **An in-process `build_server().list_tools()` as the CI smoke** — duplicates
  `test_tool_set_is_exactly_the_five` and proves nothing about the deployed image/transport (§2). Out (used
  only as the local lockstep *unit* test, not as the CI live smoke).
- **A retry loop / `continue-on-error`** on the step — a set assertion is deterministic; hiding a failure
  behind retries defeats the pin. Out.
- **An `mcp`/FastMCP version bump, a new runtime dependency, or any `src/worldmonitor/mcp/server.py` edit.**
  Out.

---

## 10. Future pin-sweep locus note (F-3 lesson — record and carry)

The F-3 lesson (ADR 0122): adding a tool means updating **every** place the tool set is pinned. This gate
**adds a new pin locus**. After F-8 merges, the loci are:

**Tool set** (any future 6th tool must update all of):
1. `tests/unit/test_mcp_server.py::test_tool_set_is_exactly_the_five` (rename it when the count changes).
2. `src/worldmonitor/mcp/smoke.py::EXPECTED_TOOLS` — **new (this gate).**
3. `tests/integration/test_mcp_stdio.py` / `test_mcp_http_transport.py` wire `tools/list` assertions.

**Prompt set** (any future 3rd prompt must update all of):
1. F-4's `test_prompt_set_is_exactly_the_two` (`tests/unit/test_mcp_prompts.py`).
2. `src/worldmonitor/mcp/smoke.py::EXPECTED_PROMPTS` — **new (this gate).**
3. The F-4 stdio/http prompt wire assertions.

**Mitigation baked in:** the §7.1 **lockstep unit test** fails the moment `smoke.py`'s constants drift from
the live registration — so a forgetful "added a tool but not `EXPECTED_TOOLS`" is caught in the fast unit
suite (not only in the slow compose-boot job). The revisit trigger is recorded in ADR 0126.

---

## 11. Slice breakdown

**ONE slice** (P2 / XS — a single package module + one appended CI step + one small unit test). There is no
sanctioned split: the smoke module and the step that invokes it are meaningless apart, and the unit test pins
the module.

- **Slice 1 — MCP live-smoke rider on compose-boot.**
  - New package module `src/worldmonitor/mcp/smoke.py`: `EXPECTED_TOOLS`, `EXPECTED_PROMPTS`, a pure
    `compare(served_tools, served_prompts) -> (int, str)` (exit code + diff message), `run_smoke()`
    (spawn `python -m worldmonitor.mcp` with `MCP_TRANSPORT=stdio`, handshake `initialize` /
    `notifications/initialized` / `tools/list` / `prompts/list`, collect sets, call `compare`, bounded read
    deadline + `finally` terminate), and `main()` → `sys.exit(run_smoke())`.
  - `.github/workflows/compose-boot.yml`: append the ONE `run:` step of §2.1 (additive only).
  - `tests/unit/test_mcp_smoke.py`: the §8 tests (the two lockstep tests + the two compare tests; the
    subprocess e2e is optional/`integration`).
  - ADR `docs/decisions/0126-mcp-live-smoke.md` → **ACCEPTED** at the merging PR; regenerate
    `docs/decisions/README.md`; this spec into `docs/reviews/`.

---

## 12. Open items for the test-author / builder

1. **Confirm F-4 (ADR 0125) is on `master` before building.** If `git log origin/master` does **not**
   include the F-4 merge, drop `EXPECTED_PROMPTS` and the `prompts/list` round-trip from `smoke.py`, drop the
   prompt lockstep test, and note the decision in the PR — the tool-set half is F-4-independent. (F-8's fleet
   is ordered after F-4; this should hold, but verify.)
2. **Reuse the stdio JSON-RPC idiom from `tests/integration/test_mcp_stdio.py`** (newline-delimited frames,
   `initialize` id 1 with `protocolVersion=LATEST_PROTOCOL_VERSION`, `notifications/initialized`, then the
   list methods). Import `LATEST_PROTOCOL_VERSION` from `mcp.types` (guaranteed in the image). Do **not**
   hand-invent a protocol version.
3. **Force `MCP_TRANSPORT=stdio` on the child** (`env=os.environ | {"MCP_TRANSPORT": "stdio"}`); the service
   env is `streamable-http` and the child must override it. Read the child's **stderr** for diagnostics on
   failure; never read the child's stdout for anything but JSON-RPC frames (STDOUT PURITY).
4. **Bound every read** (deadline ≈60 s to absorb the PyICU/followthemoney import cost) and always
   `terminate()` the child in a `finally`. A hung child must fail fast, not stall the 25-minute job.
5. **`--no-deps --profile agent -T --rm`** on the `docker compose run` (§2.1). Verify the profiled `mcp`
   service resolves to `run` locally with `act` / a real compose before relying on CI; add `--profile agent`
   if a bare `run` cannot find the service.
6. **Do NOT edit** `src/worldmonitor/mcp/server.py`, `mcp/auth.py`, the `mcp`/`zitadel` compose services, or
   any existing compose-boot step. `test_tool_set_is_exactly_the_five` and the F-4 prompt tests must stay
   green untouched.
7. **Run locally:** full `pytest -m "not integration"` (the two lockstep + two compare unit tests), then —
   if the optional e2e is included — the integration test; `ruff format --check .` repo-wide; and
   `python scripts/gen_adr_index.py --check`. If Docker/`act` is available, dry-run the compose-boot step;
   otherwise push and let CI build (the WSL box cannot build images).
8. **Person-affecting / cosign:** none required — CI wiring around a read-only surface, `person_affecting=
   false`, `human_fork=false` (ADR 0126). No pause.

---

## gate.scope (to apply at fleet start)

> Written by the **F-8 fleet** (NOT by this planning task — the F-4 fleet currently owns
> `.claude/gate.scope`; do not overwrite it now). One path glob per line; the scope-guard hook enforces it.

```
src/worldmonitor/mcp/smoke.py
tests/unit/test_mcp_smoke.py
.github/workflows/compose-boot.yml
docs/decisions/0126-mcp-live-smoke.md
docs/decisions/README.md
docs/reviews/GATE_F8_MCP_LIVE_SMOKE_SPEC.md
```
