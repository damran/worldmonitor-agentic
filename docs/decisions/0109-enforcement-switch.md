# 0109 — Runtime enforcement switch: operator-toggleable safety guards

- **Status:** ACCEPTED (2026-07-12)
- **Date:** 2026-07-12
- **human_fork:** false — a reversible, default-secure operator-config addition (settings flags; revert =
  delete the flags). No data-shape lock-in; the code default is unchanged production behaviour.
- **person_affecting:** true — the guards it can toggle (the catastrophic-merge / sensitivity park and
  the GDPR-erasure authorization) fire on exactly the person-affecting paths CLAUDE.md calls
  non-negotiable, so disabling them changes what happens to a real person's data.
- **human_cosign:** Mithat 2026-07-12 — the operator directed and authorized this change in-session
  (person_affecting:true): a single-tenant, self-hosted deploy wants to run its dev/test instance on
  test data without the merge-guard park and the erase-authorization requirement, while production stays
  strict. Findings disclosed: the code default is `strict` (production-safe out of the box); `off` is
  logged loudly at boot and per-erase so it cannot ride silently into production; provenance stamping is
  deliberately NOT toggleable (data-integrity, not a review gate — `off` would only corrupt the graph,
  no dev-speed benefit).

## Context

CLAUDE.md lists a set of **non-negotiable invariants** — the catastrophic-merge guard (never auto-merge a
sensitive entity), human sign-off on person-affecting changes, GDPR-erasure authorization. These are the
correct **production** posture. But on a single-tenant, self-hosted **dev/test** instance running on test
data, being forced through the merge-guard park and the `authorized_by` requirement is pure friction with
no subject to protect. The operator wants a switch: strict in production, off (one-by-one or all together)
in development.

Two things were conflated in the request and are separated here:
- **Development-time governance ceremony** (the person-affecting **cosign** the build fleet asks for) is
  NOT a runtime check — it is how the agent builds. It has no settings flag; the operator simply directs
  the agent to proceed without pausing. Out of scope for this ADR.
- **Runtime product guards** (this ADR) become operator config.

## Decision

Add a single **enforcement switch** to `settings.py`:

- `enforcement_profile: Literal["strict", "off"] = "strict"` — the master switch. Code default `strict`
  (unchanged production behaviour); `off` bypasses every guard below.
- Per-guard overrides `enforce_merge_guard` / `enforce_erasure_authorization` (`bool | None`, `None` =
  inherit from the profile) — flip one guard without the others.
- `is_enforced(guard)` resolves override-or-profile at each guard's choke point; `disabled_enforcements()`
  + `log_enforcement_status()` warn loudly at boot when any guard is off.

Wired at two runtime choke points:
- **Merge / sensitivity guard** (`resolution/pipeline.py`) — `off` forces the existing, tested `"alert"`
  mode: flagged clusters merge anyway (never parked/blocked), the durable `merge_alerts` trail is kept.
- **Erasure authorization** (`erasure.py::erase_source`) — `off` skips the non-blank `authorized_by`
  requirement (and logs a per-erase warning); `authorized_by` gains a `""` default so an erase can run
  without it when unenforced.

The switch is deliberately narrow: it covers the two concrete runtime guards that exist and cause the
friction. The self-improvement/promotion gate is a process gate with no concrete runtime code today
(`improvement/` is a stub); active-plugin scope tokens are a separate authorization concern. Both can be
added later the same way (a name in `_ENFORCEABLE_GUARDS` + an `enforce_<name>` field + one `is_enforced`
check) — noted, not built.

## Consequences

- Production is safe by default (must be explicitly opted out). A disabled guard is never silent — it is
  logged at boot and (for erasure) per call.
- The dev/test instance sets `ENFORCEMENT_PROFILE=off` in its local `.env` (gitignored) — off today, as
  the operator asked, without changing the code default.
- CLAUDE.md's invariants section notes that the runtime enforcement is now operator-toggleable (default
  strict) per this ADR — the invariants remain the production posture, not a hard-wired constant.

## Reversibility

Fully reversible: delete the flags + the two `is_enforced` checks and behaviour returns to always-strict.
**Revisit trigger:** if the switch is ever wanted for a multi-tenant or hosted deploy, re-evaluate whether
`off` should be permitted at all outside `development`/`test` environments (today it is operator-trusted).
