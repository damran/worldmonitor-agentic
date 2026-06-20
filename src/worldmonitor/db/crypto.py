"""Symmetric encryption for connector-instance config at rest.

Connector configs hold secrets (API keys, tokens), so they are Fernet-encrypted
before they touch the database. The key comes from the environment
(``CONFIG_ENCRYPTION_KEY``) — never hardcoded.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from worldmonitor.settings import Settings, get_settings


class ConfigCipher:
    """Encrypts/decrypts connector config blobs with a Fernet key."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("config_encryption_key is not set (CONFIG_ENCRYPTION_KEY)")
        self._fernet = Fernet(key.encode())

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ConfigCipher:
        """Build a cipher from the process settings."""
        return cls((settings or get_settings()).config_encryption_key)

    @staticmethod
    def generate_key() -> str:
        """Generate a fresh url-safe base64 Fernet key."""
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        """Return a Fernet token for ``plaintext``."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Recover the plaintext from a Fernet ``token``."""
        return self._fernet.decrypt(token.encode()).decode()
