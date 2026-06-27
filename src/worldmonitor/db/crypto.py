"""Symmetric encryption for connector-instance config at rest.

Connector configs hold secrets (API keys, tokens), so they are Fernet-encrypted
before they touch the database. The key comes from the environment
(``CONFIG_ENCRYPTION_KEY``) — never hardcoded.

Key rotation (ADR 0058): the cipher is built from a **primary** key plus zero or
more decryption-only **fallback** (old) keys via ``MultiFernet``. Encryption
always uses the primary; decryption tries the primary then each fallback, so a
token written under any currently-configured key still decrypts. With no
fallbacks the cipher is a one-key ``MultiFernet`` — behaviour identical to a
single ``Fernet``.
"""

from __future__ import annotations

from collections.abc import Sequence

from cryptography.fernet import Fernet, MultiFernet

from worldmonitor.settings import Settings, get_settings


class ConfigCipher:
    """Encrypts/decrypts connector config blobs with one or more Fernet keys."""

    def __init__(self, key: str, fallbacks: Sequence[str] = ()) -> None:
        if not key:
            raise ValueError("config_encryption_key is not set (CONFIG_ENCRYPTION_KEY)")
        keys = [Fernet(key.encode())]
        keys.extend(Fernet(f.encode()) for f in fallbacks if f)
        self._fernet = MultiFernet(keys)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ConfigCipher:
        """Build a cipher from the process settings (primary + fallback keys)."""
        resolved = settings or get_settings()
        fallbacks = _parse_fallbacks(resolved.config_encryption_key_fallbacks)
        return cls(resolved.config_encryption_key, fallbacks)

    @staticmethod
    def generate_key() -> str:
        """Generate a fresh url-safe base64 Fernet key."""
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        """Return a Fernet token for ``plaintext`` (always under the primary key)."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Recover the plaintext from a Fernet ``token`` (tries primary then fallbacks)."""
        return self._fernet.decrypt(token.encode()).decode()

    def rotate(self, token: str) -> str:
        """Re-encrypt ``token`` onto the current primary key so a fallback can be dropped."""
        return self._fernet.rotate(token.encode()).decode()


def _parse_fallbacks(raw: str) -> list[str]:
    """Split a comma/whitespace-separated fallback-key list, dropping blanks."""
    return [k for k in raw.replace(",", " ").split() if k]
