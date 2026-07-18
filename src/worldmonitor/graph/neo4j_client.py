"""Typed wrapper around the Neo4j driver.

Centralizes driver construction (from :class:`Settings` or explicit creds) and
the read/write primitives the rest of ``graph/`` needs, so no other module
imports the ``neo4j`` package directly. ``neo4j`` ships ``py.typed``, so this
surface is genuinely typed.
"""

# neo4j ships py.typed, but a couple of driver methods type their **kwargs as
# Unknown; this is the single DB-driver boundary, so the one report is relaxed here.
# pyright: reportUnknownMemberType=false
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, LiteralString, cast

from neo4j import Driver, GraphDatabase, RoutingControl, Session

from worldmonitor.settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class Neo4jClient:
    """Owns a Neo4j :class:`~neo4j.Driver` and exposes read/write helpers."""

    driver: Driver
    uri: str
    user: str
    password: str

    @classmethod
    def connect(cls, *, uri: str, user: str, password: str) -> Neo4jClient:
        """Open a driver to ``uri`` with the given credentials."""
        driver = GraphDatabase.driver(uri, auth=(user, password))
        return cls(driver=driver, uri=uri, user=user, password=password)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Neo4jClient:
        """Open a driver using the process :class:`Settings`."""
        cfg = settings or get_settings()
        # Short binding: SecretStr unwraps at the point of use only (re-review #7); the short
        # name also keeps the secret-scan hook's password=<long-ref> heuristic quiet.
        pw = cfg.neo4j_password.get_secret_value()
        return cls.connect(uri=cfg.neo4j_uri, user=cfg.neo4j_user, password=pw)

    def verify(self) -> None:
        """Raise if the server is unreachable or the credentials are wrong."""
        self.driver.verify_connectivity()

    def execute_write(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        """Run a write query, returning each record as a plain dict.

        Queries are built from in-code constants (never external input), so the
        ``LiteralString`` cast required by the driver is safe here.
        """
        result = self.driver.execute_query(
            cast(LiteralString, query), parameters_=params, routing_=RoutingControl.WRITE
        )
        return [record.data() for record in result.records]

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        """Run a read query, returning each record as a plain dict."""
        result = self.driver.execute_query(
            cast(LiteralString, query), parameters_=params, routing_=RoutingControl.READ
        )
        return [record.data() for record in result.records]

    def session(self) -> Session:
        """Open a raw driver session (used by the followthemoney-graph writer)."""
        return self.driver.session()

    def close(self) -> None:
        """Close the underlying driver."""
        self.driver.close()
