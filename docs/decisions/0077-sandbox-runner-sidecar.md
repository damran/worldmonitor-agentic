# 0077 — Sandbox-runner sidecar for heavy ACTIVE CLI tools (container + constrained egress)

- **Status:** accepted
- **Date:** 2026-06-29
- **Gate:** Stage-4 hardening — the "container/egress sandbox" deferred by ADR
  [0071](0071-active-capability-gating.md) §6 and ADR [0072](0072-clitool-dig-nmap-ui.md) §1 (both
  named it a "Stage-4 container fork"). Resolves the **isolation-primitive fork WITH the user**
  (chosen: a dedicated sandbox-runner sidecar, over Docker-out-of-Docker and unprivileged-in-container).
- **Touches (across two slices):** `settings.py`, a new `sandbox/` package (the app-side container
  runner + the sidecar service), `plugins/cli_tool.py` / `runner/operator_run.py` (route container-level
  tools through the sidecar), `deploy/compose.yaml`, `Dockerfile`, `.env.example`. **Not
  person-affecting** — operator-gated active-recon execution plumbing; no ER/merge/score/graph
  (`human_fork: false`). The **isolation-primitive choice itself was a human fork** (security-sensitive)
  and was decided with the user; the remaining knobs are reversible network/deploy policy.

## Context

ADR 0071/0072 shipped the ACTIVE-capability gate but **deferred the sandbox itself**: today
`container_sandbox_enabled` (`settings.py:195`) is a **pure refusal toggle** read in one place
(`operator_run.run_connector_once` raises `SandboxUnavailableError`→HTTP 409 for a connector whose
`sandbox=="container"`, i.e. nmap). There is **no isolation or egress mechanism anywhere** — every CLI
tool runs through `run_command` (`runner/subprocess.py`) as a **raw host subprocess** (`asyncio.
create_subprocess_exec`, argv-list/no-shell, wall-clock timeout). Flipping the flag today would run
nmap **un-sandboxed on the host** — the flag name over-promises. The deploy is a single default bridge
with unrestricted egress + mutual reachability to postgres/neo4j/minio; the app runs **non-root
(uid 10001), no caps, no docker.sock**, and the image does not even contain whois/dig/nmap.

CLAUDE.md / `10_ARCHITECTURE.md:160` lock the invariant: **heavy CLI tools run "in containers with
constrained egress."** The *which* (mechanism, egress model, limits) was open — a security-sensitive
fork, so resolved with the user.

## Decision

**A dedicated `sandbox-runner` sidecar service** owns the constrained execution. The app delegates the
execution (only) over the internal network; it never gains a Docker socket or extra capabilities.

### D1 — Architecture (the chosen primitive)
- A new in-network service `sandbox-runner` (same image, with the tool binaries added) runs a tiny
  FastAPI app exposing `POST /run` (and `/health`). The app-side **`ContainerRunner`** (a
  `Runner`-compatible callable — the existing seam `cli_tool.py:46/65-67/98`) POSTs `{argv, timeout}`
  to it and returns the same `RunResult` contract, so `collect()`/`map()` are unchanged.
- **Rejected:** Docker-out-of-Docker (mounting `docker.sock` grants ~host-root to the app — collides
  with the non-root posture); unprivileged in-container nsjail/bwrap (needs CAP_SYS_ADMIN/CAP_NET_ADMIN
  **on the app container** — weakens its hardening). The sidecar concentrates all privilege in one
  small, single-purpose, separately-hardened service.

### D2 — Defense-in-depth at the sidecar (never trust the caller)
The sidecar **re-validates independently**: `argv[0]` ∈ its own allowlist `{nmap, dig, whois}`, argv is
a **list** (no shell), timeout bounded; it executes via the same `run_command` (argv-list, no-shell,
process-group SIGKILL on timeout). The app-side validator/allowlist (ADR 0071/0072) stays; the sidecar
is a second, independent gate.

### D3 — Request protocol + auth
`POST /run` over the **internal compose network only** (no host publish), authenticated by a
**shared secret** header (`sandbox_runner_secret`, a `SecretStr` from env; constant-time compare). New
settings: `sandbox_runner_url: str = ""` (empty ⇒ not configured) and `sandbox_runner_secret`. When
`container_sandbox_enabled` is True but no URL/secret is configured, container-level tools are **still
refused** (`SandboxUnavailableError`) — the flag alone never runs nmap un-sandboxed.

### D4 — Constrained egress (the crux)
The sidecar runs with **`CAP_NET_ADMIN` (sidecar only)** and an entrypoint that applies an **nftables
OUTPUT policy: DROP to all private/internal ranges** (10/8, 172.16/12, 192.168/16, 169.254/16
link-local+cloud-metadata, 127/8 except loopback, fc00::/7, ::1) and **ACCEPT established/related + the
rest (public)**. Effect: the scanner can reach a **public scan target** (its operator-authorized
purpose) but **cannot reach our own stores (postgres/neo4j/minio/redis/zitadel) or cloud metadata, and
cannot exfiltrate to internal services** — satisfying the data-sovereignty HARD rule (our data never
routes through an external broker; only the operator-directed read of the target egresses). The
app→sidecar request is INPUT (established/related), so it is unaffected by the OUTPUT drop.
*v1 scope:* deny-internal/allow-public, **not** per-run target-only allowlisting (dynamic per-target
nftables is deferred — see Reversibility). nmap runs **non-root ⇒ TCP connect-scan** (`-sT`), so **no
`CAP_NET_RAW`** is needed.

### D5 — Resource limits
Compose caps on the sidecar: `mem_limit`, `pids_limit`, `cpus`, `ulimits` (nofile), plus the existing
wall-clock timeout. Non-root, `read_only` rootfs + a tmpfs for scratch.

### D6 — Slice plan (two independently-mergeable slices)
- **Slice 1 (this gate, verifiable locally — flag stays off in prod):** settings (`sandbox_runner_url`,
  `sandbox_runner_secret`); the app-side `ContainerRunner`; route container-level connectors through it
  (the `operator_run` gate flips from "refuse" to "refuse unless enabled **and** configured, else route
  to the sidecar runner"); the **sidecar service module** (`POST /run` + validator + `run_command`);
  full unit/integration tests with a **fake sidecar / fake runner** (no image build — matches the WSL
  "can't build images" constraint). Lands behind the default-off flag ⇒ **no prod behaviour change**.
- **Slice 2 (deploy infra, CI/compose-boot-verified):** the `Dockerfile` tool binaries; the
  `sandbox-runner` compose service (non-root, read-only, `CAP_NET_ADMIN`, nftables egress entrypoint,
  resource limits, internal network); wiring `SANDBOX_RUNNER_URL`/secret into the app; compose-boot smoke.

## Consequences
- Flipping `container_sandbox_enabled` (once the sidecar is deployed + configured) routes nmap through
  the **isolated, egress-constrained sidecar** — the locked "containers with constrained egress"
  invariant is finally backed by a runtime. whois/dig stay subprocess-level in v1 (unchanged).
- The **app keeps its non-root/no-caps/no-socket hardening**; all privilege (CAP_NET_ADMIN, tool
  binaries, public egress) is concentrated in one small, single-purpose, separately-limited service.
- **Not person-affecting**; no schema change; no migration. The sidecar is **operator-run-only** (the
  cadence driver still refuses all ACTIVE connectors, ADR 0071).
- **Local-test honesty:** the seam + service code are fully tested here with fakes; the real container
  exec + nftables egress are exercised by CI compose-boot + deploy, not this box (it can't build images).

## Slice 2 — deploy & egress (built 2026-06-29; refines §D4 and §D2)

Building Slice 2, two decisions were refined for robustness/testability (recorded here for sign-off):

**§D4 refinement — egress is enforced by Docker NETWORK ISOLATION (primary), not an in-container
nftables entrypoint.** The compose stack ran on a single default bridge; Slice 2 adds a dedicated
`sandbox_net` and puts the `sandbox-runner` service on `sandbox_net` **only** (the stores —
postgres/neo4j/minio/redis/zitadel — stay on the default network), while `api`/`driver` join **both**
(so they can POST to the sidecar). By topology the sidecar **cannot reach our internal stores** — the
core data-sovereignty goal — with **no `CAP_NET_ADMIN`, no privilege-drop entrypoint, no nftables**
(the fragile, locally-untestable part of the original D4). The sidecar stays **non-root** like the rest
of the stack. This is simpler, more robust, and far less misconfiguration-prone than nftables, and it
satisfies the locked "constrained egress" invariant (egress is constrained away from our stores).
*Trade-off:* network isolation alone does NOT block the sidecar from reaching arbitrary public IPs
(that is the scan tool's purpose) or a cloud metadata endpoint (169.254.169.254). **Deferred follow-up:**
add nftables (or a host firewall rule) to deny link-local/metadata + broad RFC1918 as defense-in-depth
when there is a way to test it / when deploying to a cloud with a metadata service. For a self-hosted
deploy (the target model) there is no metadata service, so network isolation is sufficient for v1.

**§D2 hardening — the sidecar argv allowlist is per-tool, default-deny.** Beyond `argv[0] ∈
{nmap,dig,whois}`, the sidecar requires every **middle** token (`argv[1:-1]`) to be in that tool's
fixed allow-set (`nmap`: `{-oX, -, --}`; `dig`: `{+short, --}`; `whois`: `{--}` — exactly what the
connectors emit) and the **last** token (the target) to pass the same host/IP validator the connectors
use (`[A-Za-z0-9.:-]+`, ≤253, no leading `-`, no `/`). This closes the judge's HIGH finding (a
compromised caller can no longer smuggle nmap `--script`(NSE) / `-oN`(file write) / `-iR`(random
internet scan) / `-iL`(file read) past the sidecar — they are not in the allow-set). Intentional
coupling: adding a connector flag requires updating the sidecar allow-set (a safe, explicit edit).

Slice 2 also: a `sandbox-runner` **Dockerfile stage** (`FROM runtime`, apt-installs nmap + dnsutils +
whois — a dedicated target, so the api/driver image stays slim/apt-free); the **compose service**
(non-root, `read_only` + tmpfs, `mem_limit`/`pids_limit`/`cpus`/`ulimits`, `/health` healthcheck, **no
host port** — in-network only); and wires `SANDBOX_RUNNER_URL`/`SANDBOX_RUNNER_SECRET` into api+driver.
`container_sandbox_enabled` stays **default-off** (the operator opts in once the sidecar is up).

## Reversibility
Reversible (network/deploy policy + a feature flag). **Reversal cost: low** — set
`container_sandbox_enabled=False` (or leave `sandbox_runner_url` empty) to fully refuse container tools
again (today's behaviour); remove the sidecar service. **Revisit triggers:** (1) **per-run target-only
egress** (dynamic nftables/proxy keyed on the resolved target) if deny-internal/allow-public proves too
broad for an OPSEC posture (ADR 0071 noted "active recon through controlled proxies"); (2) moving
whois/dig into the sandbox too; (3) a stronger runtime (gVisor/Kata) if container escape becomes a
modelled threat; (4) real runtime-health checking (vs the boolean flag) of the sidecar.
