"""Shared pytest fixtures, including ephemeral backing services.

The container fixtures are session-scoped and lazy — a container only starts when
a test actually requests its fixture — so the default (unit) run never touches
Docker. Integration tests opt in via ``@pytest.mark.integration``; the
``integration`` CI job is the required Phase-1 merge gate.

Locally (where the shared egress hits the Docker Hub anonymous pull limit) run
with the no-rate-limit mirror::

    TESTCONTAINERS_HUB_IMAGE_NAME_PREFIX=public.ecr.aws/docker/library/ \\
    TESTCONTAINERS_RYUK_DISABLED=true uv run pytest -m integration
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from worldmonitor.graph.neo4j_client import Neo4jClient

NEO4J_IMAGE = "neo4j:2026.05.0-community"
NEO4J_TEST_PASSWORD = "testpassword"  # pragma: allowlist secret
# MinIO isn't a Docker Hub `library/` image, so it can't use the ECR mirror
# prefix; override locally with a fully-qualified mirror (e.g. quay.io/minio/minio).
MINIO_IMAGE = os.environ.get("WM_TEST_MINIO_IMAGE", "minio/minio:RELEASE.2025-09-07T16-13-09Z")


@pytest.fixture(scope="session")
def neo4j_conn() -> Iterator[tuple[str, str, str]]:
    """Spin up an ephemeral Neo4j and yield ``(uri, user, password)``."""
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer(NEO4J_IMAGE, password=NEO4J_TEST_PASSWORD) as container:
        yield container.get_connection_url(), "neo4j", NEO4J_TEST_PASSWORD


@pytest.fixture(scope="session")
def neo4j_client(neo4j_conn: tuple[str, str, str]) -> Iterator[Neo4jClient]:
    """A connected :class:`Neo4jClient` against the ephemeral Neo4j."""
    uri, user, password = neo4j_conn
    client = Neo4jClient.connect(uri=uri, user=user, password=password)
    client.verify()
    yield client
    client.close()


@pytest.fixture
def clean_graph(neo4j_client: Neo4jClient) -> Neo4jClient:
    """Return the client after wiping all data (so tests don't bleed into each other)."""
    neo4j_client.execute_write("MATCH (n) DETACH DELETE n")
    return neo4j_client


@pytest.fixture(autouse=True)
def _isolate_postgres(request: pytest.FixtureRequest) -> Iterator[None]:
    """Truncate all relational tables before each Postgres-using test.

    The Postgres container is session-scoped, so rows would otherwise bleed across
    tests. ``tenant_id`` used to namespace each test's rows (a unique tenant per test
    + ``WHERE tenant_id`` filters); under single-tenancy (D1, ADR 0042) that isolation
    is gone, so we wipe the tables here — the relational analogue of ``clean_graph``
    for Neo4j. Tests that don't request ``postgres_dsn`` (the default unit run) skip
    this entirely, so no container is started.
    """
    if "postgres_dsn" not in request.fixturenames:
        yield
        return
    from sqlalchemy import text

    from worldmonitor.db.engine import create_all, make_engine
    from worldmonitor.db.models import Base

    engine = make_engine(request.getfixturevalue("postgres_dsn"))
    create_all(engine)  # idempotent — ensures the tables exist before TRUNCATE
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    engine.dispose()
    yield


@pytest.fixture(scope="session")
def neo4j_gds_client() -> Iterator[Neo4jClient]:
    """A connected client against an ephemeral Neo4j WITH the Graph Data Science plugin."""
    from testcontainers.neo4j import Neo4jContainer

    container = (
        Neo4jContainer(NEO4J_IMAGE, password=NEO4J_TEST_PASSWORD)
        .with_env("NEO4J_PLUGINS", '["graph-data-science"]')
        .with_env("NEO4J_dbms_security_procedures_unrestricted", "gds.*")
    )
    with container:
        client = Neo4jClient.connect(
            uri=container.get_connection_url(), user="neo4j", password=NEO4J_TEST_PASSWORD
        )
        client.verify()
        yield client
        client.close()


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Spin up an ephemeral Postgres and yield a SQLAlchemy (+psycopg) DSN."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def minio() -> Iterator[tuple[str, str, str]]:
    """Spin up an ephemeral MinIO and yield ``(endpoint, access_key, secret_key)``."""
    from testcontainers.minio import MinioContainer

    with MinioContainer(MINIO_IMAGE) as container:
        config = container.get_config()
        yield config["endpoint"], config["access_key"], config["secret_key"]
