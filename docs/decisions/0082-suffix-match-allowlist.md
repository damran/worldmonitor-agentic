# 0082 — Wildcard-subdomain entries in `allowed_targets` allowlist

- **Status:** accepted
- **Date:** 2026-06-29
- **Gate:** Stage-4 MEDIUM/LOW sweep — suffix-match allowlist (ADR 0072 follow-up).
- **Touches:** `src/worldmonitor/plugins/cli_tool.py` (new `_target_allowed` helper; updated
  `collect()` allowlist check); `tests/unit/test_cli_tool_allowlist_wildcard.py` (22 new tests).
  **No schema change** (allowlist item schemas use `{type: string}` only — no pattern constraint
  that would reject a `*.`-prefixed entry). No migration, no new runtime dependency.
  Not person-affecting (`human_fork: false`). Does NOT touch `resolve_pending` / `run_ingest` /
  ER-streaming.

## Context

ADR 0072 §2 introduced an `allowed_targets` config key on `CliToolConnector.collect()`: a non-empty
list restricts an ACTIVE CLI tool to a fixed set of targets, refusing anything not in the list
(exact-match only) before the runner is ever invoked.

This exact-match-only policy is too coarse for operators who need to allow an entire domain subtree
(e.g. every host in `example.com`) without enumerating every hostname in advance.  The fix must not
weaken the security boundary: loosening the check to prefix/substring matching would open suffix-spoof
vectors (e.g. `evil-example.com` matching `example.com`).

## Decision

### D1 — Introduce `_target_allowed(target, allowed) -> bool` (pure, unit-testable helper)

Extract the allowlist check into a module-level pure function:

```python
def _target_allowed(target: str, allowed: list[str]) -> bool:
```

**Semantics:**

1. **Empty `allowed`** → `True` (any valid target; the per-run scope token is the primary auth).
2. **Non-`*.` entry** → case-insensitive **exact** match only.  No implicit sub-domain expansion.
3. **`*.<domain>` entry** → `target` must end with `"." + domain`, i.e. it is a **strict subdomain**
   of `<domain>`.  The dot boundary is the **load-bearing security invariant**:
   - The apex `<domain>` itself does **NOT** match (`"example.com".endswith(".example.com")` is
     `False`).  An explicit exact entry is required to allow the apex.
   - A sibling without a dot (`"evil-example.com"`, `"xexample.com"`, `"notexample.com"`) does NOT
     match.
   - A suffix-spoof (`"<domain>.attacker.com"`) does NOT match (the string ends with `.attacker.com`,
     not with `.<domain>`).
4. **Malformed wildcard** (`*.` with an empty domain, or a domain that still contains `*`) is silently
   skipped — it matches nothing and can never become a catch-all bypass.

Both sides are lowercased before comparison (DNS is case-insensitive).

### D2 — Update `collect()` to call `_target_allowed`

Replace:
```python
if isinstance(allowed, list) and allowed and target not in allowed:
```
With:
```python
if isinstance(allowed, list) and not _target_allowed(cast("str", target), allowed):
```

The `ValueError` message shape is unchanged
(`"target {target!r} is not in the configured allowed_targets — refused"`).  Existing callers and
tests that depend on exact-match behavior are unaffected.

### D3 — JSON config schemas: no change required

The `allowed_targets` items in all three connector schemas (whois, dig, nmap) are typed as
`{type: string}` with no `pattern` constraint — a JSON string of the form `*.example.com` is
already valid under those schemas.  No schema loosening is necessary.

## Security invariants (the dot-boundary anchor)

These invariants are expressed as parametrized `pytest.mark.parametrize` tests in
`tests/unit/test_cli_tool_allowlist_wildcard.py` and MUST NOT be weakened, skipped, or deleted:

| Test axis | Expected result |
|---|---|
| `a.example.com`, `a.b.example.com`, `A.EXAMPLE.COM` vs `*.example.com` | MATCH |
| `example.com` (apex) vs `*.example.com` | NO match |
| `evil-example.com`, `xexample.com`, `notexample.com` vs `*.example.com` | NO match |
| `example.com.attacker.com` vs `*.example.com` | NO match |
| `example.com` vs `["example.com"]` | MATCH; `a.example.com` does NOT match |
| `*.` (empty domain) vs any target | NO match (malformed, skipped) |
| `*.*.x` (nested `*`) vs any target | NO match (malformed, skipped) |

## Alternatives considered

- **Regex-based matching (e.g. `fnmatch`):** rejected — `fnmatch.fnmatch("example.com", "*.example.com")`
  returns `True` on some platforms (it treats `*` as "zero or more characters"), which would bypass the
  apex exclusion.  The explicit `endswith("." + domain)` check is unambiguous and easier to audit.
- **Full glob support (`**.example.com`, `?` wildcards, etc.):** out of scope.  Glob expansion
  introduces a larger attack surface with no use-case justification.  A single `*.` prefix is
  sufficient for all operator sub-tree needs.
- **Schema-level pattern constraint on allowlist items:** considered but rejected — the existing
  schemas use `{type: string}` without a pattern; adding a pattern that rejects `*.` entries would
  break the feature.  The runtime logic is the enforcement layer; the schema documents the structure.

## Consequences

- Operators can now write `"allowed_targets": ["*.example.com"]` to permit any strict subdomain of
  `example.com` without enumerating hostnames.  The apex requires an explicit exact entry.
- All existing exact-match behavior is unchanged — the change is purely additive.
- No data-shape lock-in; no migration; no schema change; not person-affecting.
- 22 new unit tests in `tests/unit/test_cli_tool_allowlist_wildcard.py` (including 7 adversarial
  security-axis parametrized cases) pin the dot-boundary invariant.

## Reversibility

**Reversible.** Reversal cost: low — revert `_target_allowed` (delete the helper, restore the
`target not in allowed` one-liner in `collect()`); remove `test_cli_tool_allowlist_wildcard.py`.
No data change, no migration, nothing public-facing.

**Revisit trigger:** if an operator needs allow-any-IP-in-a-CIDR semantics (not a domain subtree),
that is a separate gate with a separate security analysis (CIDR matching is not covered here).
