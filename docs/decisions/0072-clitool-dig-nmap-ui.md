# 0072 — CliTool dig + nmap (sandbox-gated) + Run-UI + enforced allowlist/validator (Phase-2 Stage-3 slice 6b)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/3h-clitool-dig-nmap-ui` (off `master`). Finishes Stage-3's active-connector surface on the
  proven 6a base (ADR 0071). Decided with the user (all three tools; subprocess-for-v1; nmap → container-gated).
- **human_fork:** false (additive on the proven gate; choices are reversible).

## Context

6a (ADR 0071) shipped the ACTIVE gate end-to-end (scope token + operator-run + audit + `CliToolConnector` +
**whois**), with two LOW follow-ups deferred and the **heavy-tool sandbox** question parked. 6b closes them:
the **dig** + **nmap** connectors (the user chose all three), the **enforced** instance target-allowlist (6a
removed an unenforced one), the `_validate_target` hardening, and the UI **"Run (active)"** button on the
proven `POST …/run` endpoint. Per the user's sandbox choice (subprocess for v1), **nmap is built but its
execution is gated** behind a container sandbox that isn't built — so an nmap run is **refused**, demonstrating
the heavy-tool gate (whois/dig run via subprocess; the only un-sandboxed thing that runs is read-only lookups).

## Decision

### 1. Sandbox level on `CliToolConnector` + the heavy-tool gate
`CliToolConnector` gains a class attribute `sandbox: str = "subprocess"`. **nmap** sets `sandbox = "container"`.
`run_connector_once` refuses to execute a `sandbox == "container"` connector when
`settings.container_sandbox_enabled` is `False` (new setting, **default `False`** — no container runtime in
v1) — raising a clear `SandboxUnavailableError` BEFORE any subprocess/landing (the REST route maps it to **409**).
So nmap is **always refused in v1**; the refusal is the proof that a heavy tool cannot run un-sandboxed. (When
the Stage-4 container sandbox lands + the flag flips, nmap runs in it — no connector change.)

### 2. Enforced instance target-allowlist (6a follow-up)
`allowed_targets` returns to the connector config schemas, now **enforced**: `CliToolConnector.collect`, after
`_validate_target`, checks the scope target against `config.get("allowed_targets")` — if that list is present
and non-empty, the target MUST be in it (exact match), else `ValueError` (refused, REST 422). An operator can
thus pre-restrict an instance to a fixed target set; an empty/absent allowlist means "any valid target"
(the per-run scope token remains the primary authorization).

### 3. `_validate_target` hardening (6a follow-up) — a shared, stricter base validator
The strict validator moves to `CliToolConnector._validate_target` (the default; whois/dig inherit it):
accept only `^[A-Za-z0-9.:-]+$`, **no leading `-`**, **no whitespace**, **length ≤ 253**, and **reject any
`..` substring** (closing the bare-`..`/long-string gaps the 6a checker flagged). Connectors may override but
default to this.

### 4. dig connector (`plugins/connectors/dig/`) — ACTIVE, subprocess
`sandbox="subprocess"`, `Capability.ACTIVE`. scope `{target: <domain>}` (the shared validator); `_build_argv`
== `["dig", "+short", "+timeout=…", "--", target]` (list, `--`, one positional); `map()` does a minimal
fail-soft parse of the resolved records → a thin FtM entity for the queried name + its resolved addresses
(or `[]` if nothing parseable — the raw is still landed) + provenance. (DNS↔FtM is a loose fit; a richer
mapping/`wm:` extension is a noted future refinement — the gate is the point.)

### 5. nmap connector (`plugins/connectors/nmap/`) — ACTIVE, container-gated
`sandbox="container"`, `Capability.ACTIVE`. scope `{target, ports?}`; `_build_argv` builds a safe argv
(`["nmap", "-oX", "-", "--", target]` style — list, validated target). map() parses the (XML) output → FtM
(host/service entities) fail-soft. **Execution is refused in v1** (§1) — the connector exists (manifest,
schema, map, argv all tested) but `run_connector_once` won't run it until the container sandbox lands.

### 6. UI "Run" buttons (`api/integrations.py` GET + `integrations.html`)
The catalog route annotates each configured instance with its connector's **capability** (and, for ACTIVE,
its `sandbox`). The instances table renders: for an **ACTIVE** instance, a **"Run (active)"** form with a
**target input** + a hidden **CSRF** token → `POST /integrations/instances/{id}/run`; for a **PASSIVE**
instance, a **"Run now"** button (no scope). A container-gated (nmap) instance shows the button but the run
returns the 409 sandbox refusal (surfaced to the operator). All via the existing 6a `/run` endpoint
(get_principal + CSRF + the scope-token/audit flow) — no new route.

## Safety
- All the 6a invariants hold (argv-list/no-shell/`--`; scope token; operator-only path; audit; CSRF;
  read-only; never agent-auto-run). 6b only ADDS: the stricter shared validator, the enforced allowlist,
  and the container-gate that REFUSES nmap. nmap can never run un-sandboxed in v1. dig is read-only DNS.

## Alternatives considered
- **Run nmap via subprocess in v1** (the literal "subprocess for all tools" reading). Rejected as flagged
  with the user: an un-sandboxed network scanner from the host violates "heavy CLI tools in containers";
  gating its execution until the sandbox is the safe reading of "subprocess + container-deferred".
- **A `wm:DnsRecord`/`wm:Host` extension for dig/nmap.** Deferred — a thin FtM map (or land-only) suffices for
  v1; no L2 change.
- **Disable the Run button for container-gated instances.** The button + a clear 409 refusal is simpler and
  honest; disabling is a later UX nicety.

## Consequences
- Stage-3's active surface is complete: whois + dig run (subprocess, scope-gated, audited); nmap is built and
  refused-pending-sandbox; operators trigger active runs from the UI with a scope; instances can be
  target-allowlisted. **No migration** (config-schema + a setting only). **Not person-affecting. Single-tenant.**

## Reversibility
Reversible — drop the connectors/buttons; the `sandbox` attr + `container_sandbox_enabled` default-False are
inert for existing (subprocess) connectors. Revisit triggers: the container/egress sandbox (Stage-4) flips
`container_sandbox_enabled` and nmap runs; richer dig/nmap→FtM mapping; suffix-match allowlists.

## Invariant gate note
No `@given` (active-exec boundary, not an ER invariant). **Security failing-test-first:** the shared
`_validate_target` rejects the hardened set (bare `..`, > 253 chars, leading `-`, whitespace, metachars) and
accepts a plain domain/IP; the **allowlist** enforces (target not in a configured non-empty `allowed_targets`
→ refused, before the runner); **nmap execution is REFUSED** (sandbox=="container" + flag off → 409, runner
never called) while whois/dig run; dig/nmap manifests are `Capability.ACTIVE` + correct `sandbox`; `map()`
fail-soft; the UI renders a Run-active form (target + CSRF) for ACTIVE instances and a Run-now button for
PASSIVE, and a Run with no/ wrong CSRF → 403. All over a FAKE runner + injected registry — no real
whois/dig/nmap binary, no network.
