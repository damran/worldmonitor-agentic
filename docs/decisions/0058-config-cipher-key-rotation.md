# 0058 — ConfigCipher key rotation via MultiFernet

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** Phase-B #5 (`gate/config-cipher-key-rotation`) — a focused fix off `master`.
- **Addresses:** audit **M-10** — confirmed file:line in the cross-workflow Round-2 cross-examination.

## Context — the bug

`ConfigCipher` (`src/worldmonitor/db/crypto.py`) wraps a **single** `Fernet(key)`. Connector-instance
configs (API keys, tokens) are Fernet-encrypted at rest with `CONFIG_ENCRYPTION_KEY`. Rotating that key
(a routine security operation, and mandatory after a suspected leak) makes **every** stored token
undecryptable: `Fernet(new_key).decrypt(token_encrypted_with_old_key)` raises `InvalidToken`. The driver
decrypts each instance's config on every run (`runner/driver.py` `build_driver`/`_ingest_instance`), so a
rotation orphans every connector — a fleet-wide ingest outage with **no migration path**. The empty-key
rejection is correctly enforced; multi-key rotation is the missing piece.

## Decision

Build the cipher from a **primary key plus zero or more fallback (old) keys** via
`cryptography.fernet.MultiFernet`:

- `ConfigCipher.__init__(self, key: str, fallbacks: Sequence[str] = ())` →
  `MultiFernet([Fernet(key), *[Fernet(f) for f in fallbacks]])`.
  - `encrypt` always uses the **primary** (first) key (MultiFernet semantics).
  - `decrypt` tries the primary first, then each fallback in order — so a token written under any
    currently-configured key still decrypts.
- New setting `config_encryption_key_fallbacks: str = ""` — a comma/whitespace-separated list of
  **decryption-only** old keys. `from_settings` reads the primary (`config_encryption_key`) + the
  fallbacks. Empty fallbacks ⇒ a one-key `MultiFernet` ⇒ **behaviour identical to today** (old tokens
  decrypt, new tokens encrypt with the single key; backward-compatible).
- Expose `rotate(token: str) -> str` (delegating to `MultiFernet.rotate`) so an operator/job can
  re-encrypt existing tokens onto the new primary; once all configs are re-encrypted the old key can be
  dropped from the fallbacks.

**Rotation runbook (zero-downtime):** generate a new key → set it as `CONFIG_ENCRYPTION_KEY`, move the
old key into `CONFIG_ENCRYPTION_KEY_FALLBACKS` → deploy (existing tokens decrypt via the fallback, new
writes use the new key) → re-encrypt stored tokens with `rotate()` → remove the old key from fallbacks.

## Alternatives considered

- **Two distinct single-key ciphers (try/except decrypt).** Reinvents `MultiFernet` (which is exactly
  "try keys in order"); more error-prone. Rejected.
- **A versioned key table / KMS.** Over-engineered for the current phase (one env-supplied key); a KMS
  is a named future option. `MultiFernet` is the stdlib-blessed rotation primitive.
- **Parse multiple keys out of the existing `CONFIG_ENCRYPTION_KEY` (comma-separated).** Overloads one
  var with primary+old semantics and risks an operator accidentally encrypting under the wrong element.
  A separate, decryption-only `_FALLBACKS` var is clearer and keeps the primary unambiguous.

## Consequences

- `CONFIG_ENCRYPTION_KEY` can be rotated with zero downtime; old tokens keep decrypting until the
  fallback is removed. The fleet-wide-outage footgun is closed.
- Backward-compatible: with no fallbacks configured, behaviour is byte-identical to the single-Fernet
  cipher (existing deployments + tests unaffected).
- No migration; no schema change. No merge/score/guard/resolver/pipeline change. **Not person-affecting**
  (secret-at-rest handling, no ER/score decision). `human_fork: false`.
- Security note: fallback keys are still live decryption material — the runbook says to remove the old
  key from `_FALLBACKS` promptly after re-encryption, not leave it indefinitely.

## Reversibility

Reversible (config-at-rest policy). Reversal cost: low — drop the fallbacks setting + revert to a single
`Fernet`. Revisit trigger: if key management graduates to a KMS/secrets manager, replace the env-var
fallback list with the KMS key ring.
