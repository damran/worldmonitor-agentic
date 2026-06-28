"""Gate 3g — scope-token integrity: the per-run ACTIVE authorization oracle (ADR 0071 §1).

A scope token is the tamper-evident per-run authorization an ACTIVE connector needs before it may
execute. It is a Fernet-encrypted blob over the claims ``{connector_id, instance_id, scope,
operator, issued_at, expires_at, nonce}`` minted + consumed server-side in one operator-triggered
run and stored on ``task_run.scope_token`` as the audit proof of *what was authorized, by whom*. The
security properties this file pins (failing-test-first list, ADR 0071 §Invariant gate note (c)):

* a freshly minted token VERIFIES back to its claims (round-trip);
* a TAMPERED token (one flipped byte) is REJECTED;
* an EXPIRED token (ttl in the past) is REJECTED;
* a token verified against the WRONG connector id / instance id is REJECTED;
* the token does NOT carry the scope in plaintext-readable form (it is Fernet-encrypted, not
  signed-only) — a leaked token never discloses the authorized target.

LOCKED ASSUMPTIONS the builder MUST match so this oracle stays meaningful:

  ``from worldmonitor.plugins.scope_token import mint, verify, ScopeTokenError``
  ``mint(connector_id, instance_id, scope: dict, operator: str, *, ttl_seconds=3600,
        settings=...) -> str`` — Fernet over the JSON claims, key = ConfigCipher's key from settings.
  ``verify(token, *, expected_connector_id, expected_instance_id, settings=...) -> dict`` — decrypt;
        raise ``ScopeTokenError`` on tamper / expiry / connector|instance mismatch / malformed; else
        return the claims, which include ``scope`` and ``operator``.

RED on the base tree: ``worldmonitor.plugins.scope_token`` does not exist (ModuleNotFoundError).
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet

from worldmonitor.plugins.scope_token import ScopeTokenError, mint, verify

_CONNECTOR_ID = "whois"
_INSTANCE_ID = "inst-abc-123"
_OPERATOR = "operator-alice"
# A recognizable marker inside the scope so the "not stored in plaintext" assertion is unambiguous.
_SECRET_TARGET = "SECRET-TARGET-MARKER-XYZ.example.com"
_SCOPE = {"target": _SECRET_TARGET}


def _settings() -> Any:
    """A test Settings carrying a real (random) Fernet ``config_encryption_key`` — the key the scope
    token reuses (ADR 0071 §1: ConfigCipher's key for v1)."""
    from worldmonitor.settings import Settings

    return Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-session-key-scope",
        _env_file=None,  # type: ignore[call-arg]
    )


def test_mint_verify_round_trips_claims() -> None:
    """A minted token verifies back to EXACTLY the connector/instance/scope/operator it was minted
    for (the happy path the audit record relies on)."""
    settings = _settings()
    token = mint(_CONNECTOR_ID, _INSTANCE_ID, _SCOPE, _OPERATOR, settings=settings)
    assert isinstance(token, str) and token, "mint must return a non-empty token string"

    claims = verify(
        token,
        expected_connector_id=_CONNECTOR_ID,
        expected_instance_id=_INSTANCE_ID,
        settings=settings,
    )
    assert claims["connector_id"] == _CONNECTOR_ID, claims
    assert claims["instance_id"] == _INSTANCE_ID, claims
    assert claims["scope"] == _SCOPE, claims
    assert claims["operator"] == _OPERATOR, claims


def test_tampered_token_is_rejected() -> None:
    """Flipping a single byte of the token breaks Fernet's authentication -> ScopeTokenError. A
    forged/modified authorization can never pass."""
    settings = _settings()
    token = mint(_CONNECTOR_ID, _INSTANCE_ID, _SCOPE, _OPERATOR, settings=settings)

    # Flip one character mid-token (the ciphertext/HMAC region, not the trailing padding).
    idx = len(token) // 2
    flipped = "A" if token[idx] != "A" else "B"
    tampered = token[:idx] + flipped + token[idx + 1 :]
    assert tampered != token, "the tamper must actually change the token"

    with pytest.raises(ScopeTokenError):
        verify(
            tampered,
            expected_connector_id=_CONNECTOR_ID,
            expected_instance_id=_INSTANCE_ID,
            settings=settings,
        )


def test_expired_token_is_rejected() -> None:
    """A token minted with a non-positive ttl is already expired -> ScopeTokenError. The short TTL
    is the per-run bound: a stale authorization cannot be replayed later."""
    settings = _settings()
    expired = mint(
        _CONNECTOR_ID, _INSTANCE_ID, _SCOPE, _OPERATOR, ttl_seconds=-1, settings=settings
    )

    with pytest.raises(ScopeTokenError):
        verify(
            expired,
            expected_connector_id=_CONNECTOR_ID,
            expected_instance_id=_INSTANCE_ID,
            settings=settings,
        )


def test_wrong_connector_id_is_rejected() -> None:
    """A token minted for one connector must NOT verify for another — the token is bound to the
    connector it authorized."""
    settings = _settings()
    token = mint(_CONNECTOR_ID, _INSTANCE_ID, _SCOPE, _OPERATOR, settings=settings)

    with pytest.raises(ScopeTokenError):
        verify(
            token,
            expected_connector_id="not-whois",
            expected_instance_id=_INSTANCE_ID,
            settings=settings,
        )


def test_wrong_instance_id_is_rejected() -> None:
    """A token minted for one instance must NOT verify for another — the token is bound to the
    instance it authorized."""
    settings = _settings()
    token = mint(_CONNECTOR_ID, _INSTANCE_ID, _SCOPE, _OPERATOR, settings=settings)

    with pytest.raises(ScopeTokenError):
        verify(
            token,
            expected_connector_id=_CONNECTOR_ID,
            expected_instance_id="some-other-instance",
            settings=settings,
        )


def test_token_does_not_leak_scope_in_plaintext() -> None:
    """The token is Fernet-ENCRYPTED, not merely signed: the authorized target never appears in the
    token string. A leaked/at-rest token discloses nothing about what was authorized."""
    settings = _settings()
    token = mint(_CONNECTOR_ID, _INSTANCE_ID, _SCOPE, _OPERATOR, settings=settings)

    assert _SECRET_TARGET not in token, "the scope target leaked into the token in plaintext"
    assert _OPERATOR not in token, "the operator leaked into the token in plaintext"
    assert _INSTANCE_ID not in token, "the instance id leaked into the token in plaintext"


def test_token_minted_under_one_key_does_not_verify_under_another() -> None:
    """Two processes with DIFFERENT config_encryption_keys cannot cross-validate each other's tokens
    (the token's authenticity is bound to the key)."""
    minting = _settings()
    other = _settings()  # a fresh, independent Fernet key
    token = mint(_CONNECTOR_ID, _INSTANCE_ID, _SCOPE, _OPERATOR, settings=minting)

    with pytest.raises(ScopeTokenError):
        verify(
            token,
            expected_connector_id=_CONNECTOR_ID,
            expected_instance_id=_INSTANCE_ID,
            settings=other,
        )
