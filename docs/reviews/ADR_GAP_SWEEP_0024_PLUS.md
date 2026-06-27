# ADR gap-sweep — 0024 onward (Phase E)

**Date:** 2026-06-27 · **Scope:** ADRs 0024–0059 (36 ADRs). Extends the prior production audit, which
swept only **0016–0023** for the same failure mode. Read-only analysis — this is a *list*, not a
decision; no human adjudication required.

## The failure mode hunted
A **load-bearing claim accepted on framing and never validated** — the ADR's central decision rests on a
checkable assertion that was taken as true without cited evidence, and that assertion *could* be wrong.

Each ADR's single most load-bearing claim is classified as:
- **values-choice** — a preference/policy with no truth value (not falsifiable; fine).
- **external-fact (verified)** — a checkable fact the ADR *evidenced* (cites a test, `VERIFIED_API.md`,
  file:line, or version). Already grounded.
- **empirically-testable-but-UNVALIDATED** — a checkable fact the decision relies on, asserted without a
  cited test/evidence. **This is the gap class.**

## Result

**35 of 36 ADRs are clean** — either a genuine values-choice (0024's deciding claim) or an external-fact
backed by an on-point, cited test in `tests/` and/or `VERIFIED_API.md`. The recent gate ADRs (0042–0059)
are uniformly well-grounded; no gaps were manufactured. The gate discipline (failing-test-first +
`VERIFIED_API.md` for library bindings + adversarial checker) materially avoided the framing-acceptance
failure the original audit found in 0016–0023.

**1 actionable gap: ADR 0033.** (Full per-ADR classification table is in the PR's sweep run; the
clean rows each cite their validating test, e.g. 0030→`test_migrations.py`, 0045→`test_provenance_merge.py:206`,
0047→`test_sensitivity_guard*.py`, 0057→`test_ssrf_guard.py`, 0058→`test_config_cipher.py`.)

### GAP — ADR 0033 (Neo4j bounded memory)
- **Unvalidated load-bearing claim:** the *root-cause diagnosis* that the `connection refused` / Bolt-
  never-binds failure was caused by **unbounded JVM heap auto-sizing (~25% of WSL2 RAM) + an uncapped
  container → OOM-kill before Bolt binds.** The whole remedy (bound heap, `mem_limit`, hold
  `heap_max + pagecache + ~1g < mem_limit`) is the fix *for that specific cause*.
- **Why it's a gap:** the ADR itself states the cause "cannot be Docker-verified in the build
  environment," and the *next* ADR (0034) attributes the actual crash-loop to a **different** defect
  (a bare `NEO4J_PASSWORD` rejected by strict config validation). So the heap diagnosis was never
  isolated or reproduced — it may be incidental to the real fix.
- **Partial coverage:** `.github/workflows/compose-boot.yml` boots the *fixed, bounded* config on a
  ~16 GB runner and asserts Neo4j binds Bolt :7687 — this proves the bounded config parses+boots, but
  never reproduces the OOM on a constrained host nor proves heap was the cause. No `tests/` file covers it.
- **Validating check (filed as an issue):** boot the *pre-fix unbounded* config in a memory-cgroup-
  limited container (`mem_limit` ≈ the fraction that triggered it) and assert OOM-kill / no-bind, versus
  the bounded config binding cleanly — a controlled A/B that isolates heap as the cause.
- **Severity:** LOW. The bounded config is correct and operational regardless of whether heap was the
  *sole* cause; this is diagnosis-confidence debt, not a live defect. Tracked, not gating.

## Non-gap doc nits (worth a cleanup, not gaps)
- **ADR 0028** cites a stale test name (`test_resolver_is_isolated_per_batch_no_cross_tenant_leak`;
  actual `test_resolver_is_isolated_per_batch` after the 0042 single-tenancy rename) — coverage holds.
- **ADR 0050** (JSONB→JSON round-trip) and **ADR 0051** (`restart: unless-stopped`) have untested
  *secondary* sub-claims; neither is the load-bearing claim, and 0051's is standard Compose behaviour.

## Outcome
The single gap (ADR 0033 root-cause validation) is filed as a tracked issue. No ADR requires reopening;
no decision is overturned. This sweep closes the Phase-E directive to extend the framing-acceptance audit
past 0023.
