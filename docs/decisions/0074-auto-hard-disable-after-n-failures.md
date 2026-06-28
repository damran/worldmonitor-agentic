# 0074 — Auto-hard-disable a connector instance after N consecutive failures

- **Status:** accepted
- **Date:** 2026-06-28
- **Gate:** H-8a (Stage-4 hardening). The first of the H-8 remaining halves; **extends ADR
  [0054](0054-driver-connector-retry-backoff.md)** (the retry/backoff half).
- **Touches:** `runner/driver.py` (`_finalize`), `settings.py`. **Not person-affecting** — pure
  connector *scheduling*; touches no ER/merge/score/guard/graph path (`human_fork: false`).

## Context

ADR 0054 made a failed ingest instance **retry forever** on a capped exponential backoff (so it never
goes silent), and explicitly deferred **"auto-hard-disable after N failures"** as a named follow-up —
reserving the `error` instance status for exactly it (0054 §"Alternatives", §Consequences). A
*permanently* broken connector (revoked credentials, a deleted source, a persistent 4xx) therefore
retries forever at the hourly cap: harmless, but noisy, and an operator has **no terminal signal**
that "this one is dead — fix it or remove it" versus "this one is just slow / transiently failing."

## Decision

After **`ingest_max_consecutive_failures`** consecutive ingest failures, **hard-disable** the instance:
set `status="error"` (a **terminal** state — the due-query selects only `status=="enabled"`, so it
stops retrying) instead of the `enabled`+backoff path. Concretely, in `_finalize`'s failure branch:

- compute the consecutive-failure streak — the **same** `task_run`-derived count ADR 0054 already uses
  for the backoff (**no schema change**, no new column);
- if `ingest_max_consecutive_failures > 0` **and** the streak `>=` it → `instance.status = "error"`
  (hard-disabled; `next_run` is left untouched and is moot since the status no longer matches the
  due-query), and log at `error` level;
- otherwise → the unchanged ADR-0054 behaviour (`status="enabled"`, `next_run = now + backoff`).

The **failure stays visible**: the `task_run` row is still written `status="error"` with its reason,
so run history (and the H-8 `/metrics` half) still sees every failure. An operator **re-enables** a
hard-disabled instance via the existing Integrations-UI **enable** button (`status → "enabled"`,
ADR 0069), and the streak naturally resets on the next success.

New setting (next to the ADR-0054 retry settings):
- `ingest_max_consecutive_failures: int = Field(default=10, ge=0)` — hard-disable threshold.
  **`0` disables hard-disable entirely** (retains the exact ADR-0054 retry-forever behaviour). The
  default `10` ≈ a few minutes of fast retries, then the hourly cap for several hours, before giving up.

**`error` vs `disabled` (state model).** `disabled` stays the **operator-off** state (the UI
enable/disable toggle, default for a freshly-created instance). `error` is the **system-hard-disabled**
terminal state — needs operator attention — exactly the reservation ADR 0054 made. `running` and
`enabled` are unchanged.

## Consequences

- A permanently-broken connector now **stops after a bounded number of attempts** and surfaces a
  terminal `error` state, instead of retrying forever — while a transient failure still self-heals
  (the streak resets on any success, so it never reaches the threshold).
- The ADR-0054 behaviour for failures `1 .. N-1` is **unchanged** (still `enabled` + backoff). The
  default `N=10` is far above the 1–2 failures the existing driver tests drive, so **no test-contract
  flip** (unlike ADR 0054, which had to invert one assertion).
- **No migration** — `status` is an existing column and `"error"` is just a new value it can hold
  (`db/models.py` untouched; the migration drift-guard is not triggered).
- **Not person-affecting** — connector scheduling only; no per-run human sign-off.
- **Re-enable semantics.** The streak is derived from run history and resets only on a *success*
  (consistent with ADR 0054), so re-enabling a still-broken instance hard-disables again on its next
  failed attempt — the operator re-enables once the fault is actually fixed (the next success then
  resets the streak). Giving re-enable a fresh N-attempt budget would need a streak boundary written
  in the UI enable path (`api/integrations.py`), which is **out of this gate's blast radius** —
  deferred as a small follow-up if it proves annoying in practice.

## Reversibility

Reversible (scheduling policy). Reversal cost: low — revert the `_finalize` failure branch + drop the
setting (or ship `default=0`). **Revisit trigger:** when the H-8 `/metrics` + external-alerting half
lands (ADR 0076), an operator can be paged *before* the threshold is hit, so `N` can be tuned down (or
the hard-disable folded into an alert-then-disable policy).
