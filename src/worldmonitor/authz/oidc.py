"""Zitadel OIDC token verification and the request principal.

A :class:`TokenVerifier` validates a bearer JWT against Zitadel's published
signing keys (JWKS) and returns its claims. :class:`Principal` is the
authenticated identity attached to each request. The platform is single-tenant
(D1, ADR 0042).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import jwt
from jwt import PyJWKClient


class InvalidTokenError(Exception):
    """Raised when a bearer token cannot be verified."""


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller derived from a verified token."""

    subject: str
    claims: Mapping[str, Any]

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> Principal:
        return cls(
            subject=str(claims.get("sub", "")),
            claims=dict(claims),
        )


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a bearer token and returns its claims (or raises)."""

    def verify(self, token: str) -> Mapping[str, Any]: ...


class ZitadelTokenVerifier:
    """Verifies RS256 JWTs against Zitadel's JWKS endpoint.

    ``PyJWKClient`` fetches and caches signing keys, so verification only hits
    the network on a cache miss / key rotation.
    """

    def __init__(self, *, issuer: str, jwks_uri: str, audience: str) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_client = PyJWKClient(jwks_uri)

    def verify(self, token: str) -> Mapping[str, Any]:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
            raise InvalidTokenError(str(exc)) from exc
        return claims
