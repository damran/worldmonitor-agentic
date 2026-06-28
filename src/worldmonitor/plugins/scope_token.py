"""Scope token — the tamper-evident per-run ACTIVE authorization (ADR 0071 §1).

A scope token is the authorization an ACTIVE connector needs before it may execute. It is a
Fernet-encrypted blob (reusing :class:`worldmonitor.db.crypto.ConfigCipher`'s key for v1) over the
claims ``{connector_id, instance_id, scope, operator, issued_at, expires_at, nonce}``. It is minted
and consumed server-side in one operator-triggered run and stored on ``task_run.scope_token`` as the
audit proof of *what was authorized, by whom* — it is NOT a wire credential handed to a client.

Security properties (ADR 0071 §Invariant gate note (c)):

* a freshly minted token verifies back to its claims (round-trip);
* a TAMPERED token (one flipped byte) fails Fernet's authentication → :class:`ScopeTokenError`;
* an EXPIRED token (``now > expires_at``) is rejected;
* a token verified against the WRONG connector id / instance id is rejected (bound to what it
  authorized);
* the token is ENCRYPTED, not merely signed: the authorized target never appears in plaintext, and a
  token minted under one key never validates under another (authenticity is bound to the key).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.settings import Settings


class ScopeTokenError(ValueError):
    """Raised when a scope token cannot be verified (tamper / expiry / mismatch / malformed)."""


def mint(
    connector_id: str,
    instance_id: str,
    scope: dict[str, Any],
    operator: str,
    *,
    ttl_seconds: int = 3600,
    settings: Settings | None = None,
) -> str:
    """Mint a Fernet-encrypted scope token authorizing one ACTIVE run.

    The claims bind the run to ``connector_id`` + ``instance_id`` + ``operator`` + ``scope`` and are
    valid for ``ttl_seconds`` (a short per-run window; a non-positive ttl yields an already-expired
    token). The ``nonce`` makes every minted token unique even for an identical scope.
    """
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "connector_id": connector_id,
        "instance_id": instance_id,
        "scope": dict(scope),
        "operator": operator,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        "nonce": uuid.uuid4().hex,
    }
    return ConfigCipher.from_settings(settings).encrypt(json.dumps(claims))


def verify(
    token: str,
    *,
    expected_connector_id: str,
    expected_instance_id: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Decrypt + validate ``token`` and return its claims, or raise :class:`ScopeTokenError`.

    Rejects (a) a token that fails Fernet decryption (tampered, malformed, or minted under a
    different key); (b) a malformed payload; (c) a connector/instance the token was not minted for;
    (d) an expired token (``now > expires_at``). On success the claims (including ``scope`` and
    ``operator``) are returned for the audit record.
    """
    cipher = ConfigCipher.from_settings(settings)
    try:
        plaintext = cipher.decrypt(token)
    except Exception as exc:  # cryptography raises InvalidToken / TypeError on a bad token.
        raise ScopeTokenError(
            "scope token failed to decrypt (tampered, malformed, or wrong key)"
        ) from exc

    try:
        decoded: Any = json.loads(plaintext)
    except (ValueError, TypeError) as exc:
        raise ScopeTokenError("scope token payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ScopeTokenError("scope token payload is not a claims object")
    claims = cast("dict[str, Any]", decoded)

    if claims.get("connector_id") != expected_connector_id:
        raise ScopeTokenError("scope token is not bound to this connector")
    if claims.get("instance_id") != expected_instance_id:
        raise ScopeTokenError("scope token is not bound to this instance")

    expires_raw = claims.get("expires_at")
    try:
        expires_at = datetime.fromisoformat(str(expires_raw))
    except (TypeError, ValueError) as exc:
        raise ScopeTokenError("scope token has no valid expiry") from exc
    if datetime.now(UTC) > expires_at:
        raise ScopeTokenError("scope token has expired")

    return claims
