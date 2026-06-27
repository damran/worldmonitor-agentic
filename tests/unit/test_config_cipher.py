"""Unit tests for ``ConfigCipher`` key rotation via ``MultiFernet`` (ADR 0058, Gate Phase-B #5).

``ConfigCipher`` encrypts connector-instance config (API keys, tokens) at rest. Rotating
``CONFIG_ENCRYPTION_KEY`` must NOT orphan tokens written under the old key: the cipher must accept a
primary key plus zero or more decryption-only fallback (old) keys, encrypt under the primary, and
decrypt under any configured key. ``rotate()`` re-encrypts an old-key token onto the new primary.

These tests are deterministic (no network/DB); keys are minted with ``ConfigCipher.generate_key()``.
Tests 1-2 lock the preserved single-key behaviour; tests 3-6 lock the new rotation contract.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.settings import Settings


def test_single_key_round_trip() -> None:
    """Backward-compat: a single-key cipher decrypts what it encrypted."""
    k1 = ConfigCipher.generate_key()
    cipher = ConfigCipher(k1)
    assert cipher.decrypt(cipher.encrypt("secret")) == "secret"


def test_empty_primary_key_rejected() -> None:
    """Preserved behaviour: an empty primary key fails fast."""
    with pytest.raises(ValueError):
        ConfigCipher("")


def test_rotation_old_token_still_decrypts() -> None:
    """THE load-bearing test: a token written under the old key decrypts via the fallback.

    Old token is produced by a single-key cipher under ``k1`` (the pre-rotation deployment). The
    rotated cipher has primary ``k2`` + fallback ``[k1]`` and must recover the original plaintext.
    """
    k1 = ConfigCipher.generate_key()
    k2 = ConfigCipher.generate_key()
    old = ConfigCipher(k1).encrypt("api-token")

    rot = ConfigCipher(k2, fallbacks=[k1])

    assert rot.decrypt(old) == "api-token"


def test_rotation_new_writes_use_primary() -> None:
    """A rotated cipher encrypts with the PRIMARY (k2), not a fallback (k1)."""
    k1 = ConfigCipher.generate_key()
    k2 = ConfigCipher.generate_key()
    rot = ConfigCipher(k2, fallbacks=[k1])

    new_token = rot.encrypt("x")

    # A cipher holding ONLY the new primary can read it -> encrypt used k2.
    assert ConfigCipher(k2).decrypt(new_token) == "x"
    # A cipher holding ONLY the old key cannot -> encrypt did NOT fall back to k1.
    with pytest.raises(InvalidToken):
        ConfigCipher(k1).decrypt(new_token)


def test_from_settings_reads_fallbacks() -> None:
    """``from_settings`` builds a primary + fallback cipher from the process settings.

    The settings carry primary ``k2`` and decryption-only fallback ``k1``. The resulting cipher must
    decrypt a ``k1``-encrypted token and encrypt new tokens under ``k2``.
    """
    k1 = ConfigCipher.generate_key()
    k2 = ConfigCipher.generate_key()
    old = ConfigCipher(k1).encrypt("api-token")

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        config_encryption_key=k2,
        config_encryption_key_fallbacks=k1,
    )
    cipher = ConfigCipher.from_settings(settings)

    # Old token decrypts via the configured fallback.
    assert cipher.decrypt(old) == "api-token"
    # New writes use the primary k2 (readable by a k2-only cipher, not a k1-only cipher).
    new_token = cipher.encrypt("y")
    assert ConfigCipher(k2).decrypt(new_token) == "y"
    with pytest.raises(InvalidToken):
        ConfigCipher(k1).decrypt(new_token)


def test_rotate_reencrypts_onto_primary() -> None:
    """``rotate()`` re-encrypts an old-key token onto the new primary so the fallback can be dropped."""  # noqa: E501
    k1 = ConfigCipher.generate_key()
    k2 = ConfigCipher.generate_key()
    old = ConfigCipher(k1).encrypt("api-token")

    rot = ConfigCipher(k2, fallbacks=[k1])
    rotated = rot.rotate(old)

    # The re-encrypted token is now readable by a cipher holding ONLY the new primary.
    assert ConfigCipher(k2).decrypt(rotated) == "api-token"
    # And the old key alone can no longer read it.
    with pytest.raises(InvalidToken):
        ConfigCipher(k1).decrypt(rotated)
