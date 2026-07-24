# 0126 — MCP live-smoke in CI (stdio `tools/list` + `prompts/list` set-pin on compose-boot)

- **Status:** PROPOSED (→ ACCEPTED at the gate-completing PR — the 0117–0125 convention)
- **Date:** 2026-07-24
- **human_fork:** false — a reversible, purely **additive** CI check (one appended `compose-boot` step + a
  small package smoke module) that asserts the **already-registered** MCP surface. No product/architecture
  fork: it changes no runtime behaviour, no data shape, nothing public-facing. The one genuine design choice
  (assert over **stdio** rather than authenticated HTTP) is forced by what `compose-boot` can boot (no
  Zitadel) — not a fork — and is cheaply reversible (below). Not marked OPEN.
- **person_affecting:** false — the smoke performs **no** inference/scoring/attribution/ER, changes **no**
  ER threshold / guard mode / sensitivity park / model or param, has **zero** egress, and **writes nothing**
  (no table, no migration, no graph write, no Neo4j connection at all — `tools/list`/`prompts/list` call no
  tool). It reads the tool/prompt **surface** (names), never any entity data or provenance about a person.
- **human_cosign:** not required — reversible, non-person-affecting CI wiring around a read-only surface
  (per the cost directive: reserve cosign for irreversible / person-affecting changes).
- **Backlog/roadmap:** `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-8** (P2 / XS, "XS rider on
  compose-boot").
- **Spec:** `docs/reviews/GATE_F8_MCP_LIVE_SMOKE_SPEC.md`.
- **Builds on:** ADR 0034 (compose-boot exists to catch deploy-config defects testcontainers miss), 0051
  (compose-boot builds the app image + waits api/driver healthchecks), 0063 (stdio FastMCP; trust boundary =
  who may spawn the process, no per-call token — single-tenant D1/0042), 0088 (`alert-rules` promtool-in-CI:
  a CI check with a checked-in, locally-testable artifact), 0090 (authenticated HTTP MCP transport), 0122
  (5th tool `get_entity_dossier`; F-3 pin-sweep lesson), 0125 / Gate F-4 (the two prompts `entity-workup` +
  `freshness-audit`).

## Context

The MCP surface is drift-sensitive: an accidentally-registered sixth tool (e.g. a raw-Cypher escape hatch),
a renamed tool, or an import error that drops the whole surface would ship undetected. The unit suite pins
the set **from source** (`test_tool_set_is_exactly_the_five`, and F-4's `test_prompt_set_is_exactly_the_two`)
but nothing asserts the set **as served by the actual deployed image**. `compose-boot` is exactly the vehicle
for that class of "the real artifact, not testcontainers" check (ADR 0034/0051), and it is a
branch-protection-required check.

Three facts (verified in-repo) shape the decision:

1. **`compose-boot` boots no Zitadel and no `mcp` service.** It boots `postgres neo4j minio`, then
   `api driver` (+ the `migrate`/`minio-init` one-shots), then `sandbox-runner`. The `mcp` service is in
   the `agent` profile and is never started; `zitadel` is never started.
2. **The deployed `mcp` service runs the authenticated HTTP transport and hard-fails without Zitadel.**
   `MCP_TRANSPORT=streamable-http` + `main()` raises at boot unless `ZITADEL_DOMAIN` is set, and the service
   `depends_on zitadel service_healthy`. Minting a real `worldmonitor:read` bearer would require booting
   **and** provisioning Zitadel (`zitadel_provision.sh`) — a multi-service auth bring-up, not an XS rider.
3. **The stdio transport needs no bearer and no store.** With `MCP_TRANSPORT=stdio`, `main()` runs
   `build_server().run(transport="stdio")` — no verifier, no port. `build_server()` builds a **lazy** Neo4j
   driver (no connection; `verify_connectivity` is a separate, uncalled method), and `tools/list`/
   `prompts/list` call no tool, so Neo4j is never contacted. The trust boundary is who may spawn the process
   (ADR 0063, single-tenant).

## Decision

**D1 — Add one appended `compose-boot` step that runs a live MCP smoke against the deployed image over the
stdio transport, and fail the job on surface drift.** The step runs the deployed `worldmonitor-app:dev`
image's own MCP entrypoint under stdio and asserts the served surface is **exactly** the 5 tools +
2 prompts.

**D2 — Implement the smoke as a package module `src/worldmonitor/mcp/smoke.py`
(`python -m worldmonitor.mcp.smoke`), not a `scripts/…` file and not inline YAML.** The `Dockerfile` copies
only `src/` (not `scripts/`) into the image, so the smoke must be an installed package module to run inside
the container via `docker compose run mcp …` (the `worldmonitor.runner.smoke_metrics` precedent). This also
makes the pins locally unit-testable.

**D3 — The smoke speaks real JSON-RPC over stdio to a freshly-spawned server subprocess** (forcing
`MCP_TRANSPORT=stdio` on the child), reusing the newline-delimited-frame handshake idiom of
`tests/integration/test_mcp_stdio.py` and `LATEST_PROTOCOL_VERSION` from the same installed `mcp` package.
`initialize → notifications/initialized → tools/list → prompts/list`, collect names, assert set-equality
against hardcoded `EXPECTED_TOOLS`/`EXPECTED_PROMPTS`, exit non-zero + print the symmetric diff on any
mismatch, timeout/spawn failure, or handshake error.

**D4 — Assert BOTH the tool set and the prompt set.** The backlog predates prompts (F-4); adding
`prompts/list` is a cheap, natural extension of the same drift pin and is included. (Dropped only if F-4 is
not yet on `master` at build time.)

**D5 — Additive-only; no existing `compose-boot` step is modified, reordered, or weakened; no
`mcp`/`zitadel` compose service is touched.** `compose-boot` is branch-protection-required — the change can
only make the gate stricter (fail on drift), never looser.

**D6 — A local lockstep unit test ties the smoke's hardcoded pins to the live registration** (`smoke.
EXPECTED_* == build_server().list_tools()/.list_prompts()` names), so the pins cannot silently rot and a
future added tool/prompt is caught in the fast unit suite as well as in compose-boot. No mandatory `@given`:
F-8 touches no CLAUDE.md invariant — this omission is a recorded decision, not a gap (the compose-boot job
plus the lockstep test are the reality-check).

## Alternatives considered

- **A1 — In-process `build_server().list_tools()` inside the image as the CI smoke.** Rejected as the *CI*
  smoke: it duplicates `test_tool_set_is_exactly_the_five` and proves nothing about the deployed
  entrypoint/transport actually serving the set. Retained only as the shape of the **local lockstep unit
  test** (D6), where that in-process comparison is exactly the right, cheap, non-flaky pin.
- **A2 — Authenticated HTTP `tools/list` (the backlog's literal wording).** Rejected for compose-boot: it
  requires booting **and** provisioning Zitadel to mint a real bearer — a multi-service auth bring-up, far
  beyond an XS rider — and it re-proves the 401/403/200 boundary already pinned by
  `tests/integration/test_mcp_http_transport.py`. The "authenticated" qualifier is honestly degraded to a
  stdio (no-auth-transport) live smoke. **Revisit trigger:** if a compose-boot test-auth profile is ever
  added for another reason, append a `401-without-bearer` HTTP assertion then.
- **A3 — A stdio smoke plus an HTTP-expect-401 assertion (task menu option c).** Rejected: the 401 half
  still needs the `mcp` HTTP service, which `depends_on zitadel service_healthy` and hard-raises without
  `ZITADEL_DOMAIN`; we do not relax the service's depends_on or its fail-closed boot guard (both correct
  production posture) for a CI convenience.
- **A4 — A `scripts/smoke/*.py` file invoked in the workflow.** Rejected: `scripts/` is not copied into the
  image, so it could not run inside the `mcp` container; and it would not be as directly unit-testable as a
  package module.
- **A5 — Inline Python in the workflow YAML.** Rejected: not locally testable, not reusable, and an awkward
  place to maintain a set-pin.

## Consequences

- `compose-boot` gains a live proof that the deployed image serves **exactly** the 5 tools + 2 prompts;
  drift (extra, missing, or renamed member) turns the required check red with a one-glance diff.
- A **new pin locus** exists (`smoke.py::EXPECTED_TOOLS`/`EXPECTED_PROMPTS`). Any future 6th tool / 3rd
  prompt must update it alongside the existing unit/wire pins (F-3 lesson). The D6 lockstep unit test
  enforces this in the fast suite so it cannot be forgotten silently.
- Slightly longer `compose-boot` runtime (one short stdio handshake, no store I/O). Bounded and well within
  the 25-minute job budget.
- No runtime/product change; the MCP tool/prompt behaviour is untouched.

## Reversibility

**Fully reversible, near-zero cost.** Deleting the appended `compose-boot` step, the `smoke.py` module, and
the unit test removes the check with no runtime effect (nothing depends on the smoke at runtime). Because
`compose-boot` is a required check, removal is a config/CI edit only, not a data or API change.

- **Reversal cost:** trivial (delete one CI step + one module + one test; regenerate the ADR index).
- **Revisit triggers:** (1) a compose-boot test-auth profile becomes available for other reasons → add the
  HTTP 401 assertion (A2); (2) the MCP surface count changes → update `EXPECTED_*` (the lockstep test will
  demand it); (3) the smoke proves flaky → fix the smoke (never relax an existing compose-boot step — D5).

## Invariant-gate note (property-test discipline)

F-8 touches **no** CLAUDE.md invariant (no provenance-stamping change, no ER/merge/threshold, no canonical-id
resolution, no guard, no write, no egress) — the mandatory-`@given` rule does not apply. The **compose-boot
job itself** (the live smoke) plus the **lockstep unit test** (D6) are the reality-check. Recording this as a
deliberate decision, not an omission (the `alert-rules`/`adr-index` CI-check precedent).

## Person-affecting reasoning

The smoke enumerates tool/prompt **names** over a local stdio transport and compares them to a hardcoded set.
It opens no Neo4j connection, calls no tool, reads no entity or provenance, performs no inference/scoring/ER,
makes no live-system change (no threshold/guard/model/param), and has zero egress. There is no path by which
it affects a real person. `person_affecting=false`, `human_cosign=false`.
