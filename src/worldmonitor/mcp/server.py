"""Graph-read FastMCP **stdio** server (ADR 0063, slice 2b).

The MCP twin of slice 2a's REST routes: it re-exposes the resolved graph over a
FastMCP stdio server with exactly four structured, read-only, bounded, parameterized
tools — ``get_entity`` / ``get_neighbors`` / ``get_provenance`` / ``find_paths`` —
each wrapping the SAME ``graph/queries.py`` helper and the SAME shared
``graph/read_guards`` (hop clamp + id validation) as REST. There is **no** raw-Cypher
tool (same call as ADR 0062).

HARD INVARIANTS
- **STDOUT PURITY.** Over stdio, stdout is the JSON-RPC frame channel; any non-frame
  byte corrupts it. So all logging is routed to **stderr only** (an explicit
  ``StreamHandler(sys.stderr)`` on the ``worldmonitor`` logger AND the root logger), and
  this package never calls ``print()``. Holds on the error/exception path too: a tool
  that logs-and-raises writes its diagnostic to stderr and surfaces a JSON-RPC error
  frame on stdout (never a traceback).
- **READ-ONLY.** Every tool calls ``client.execute_read`` only (via the query helpers);
  no write/MERGE/SET/DELETE, no write session.
- **NO INJECTION.** Each id is validated by shape (``read_guards.validate_entity_id``)
  BEFORE any ``execute_read``; a valid id is passed as a BOUND parameter, never spliced
  into the Cypher string.
- **BOUNDED.** ``hops``/``max_hops`` clamp to the SHARED ``read_guards.HOP_CAP`` (never a
  re-implemented literal); ``find_paths`` carries an inherited result ``LIMIT``.

Auth/transport (ADR 0063): stdio v1 (no network port); the trust boundary is who may
spawn the process inside the single-tenant deployment (D1, ADR 0042) — no per-call token.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from worldmonitor.authz.oidc import ZitadelTokenVerifier
from worldmonitor.graph import queries, read_guards
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.mcp.auth import ZitadelMCPTokenVerifier, build_auth_settings
from worldmonitor.settings import get_settings

logger = logging.getLogger("worldmonitor.mcp.server")

# Marker on the handler this module installs, so re-running ``configure_stderr_logging``
# is idempotent (never stacks a second handler).
_WM_STDERR_HANDLER = "_wm_mcp_stderr_handler"


def _ensure_stderr_handler(target: logging.Logger) -> None:
    """Attach exactly one stderr ``StreamHandler`` to ``target`` (idempotent)."""
    for handler in target.handlers:
        if getattr(handler, _WM_STDERR_HANDLER, False):
            return
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _WM_STDERR_HANDLER, True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    target.addHandler(handler)


def configure_stderr_logging() -> None:
    """Route ALL logging to stderr only — idempotent; never stdout (STDOUT PURITY).

    Installs a single ``StreamHandler(sys.stderr)`` on both the ``worldmonitor`` logger
    (our own diagnostics) and the root logger (so a chatty dependency / the mcp SDK can't
    leak to stdout either). Never calls ``logging.basicConfig`` (which could target
    stdout) and the ``mcp`` package never ``print()``s. Calling this twice does NOT add a
    duplicate handler.
    """
    wm_logger = logging.getLogger("worldmonitor")
    wm_logger.setLevel(logging.INFO)
    _ensure_stderr_handler(wm_logger)
    _ensure_stderr_handler(logging.getLogger())


def _tool_error(error: str, hint: str) -> ToolError:
    """Build a ``ToolError`` whose message is a JSON ``{"error", "hint"}`` envelope.

    Raise-based (ADR 0121 D3): the SDK re-wraps any raised exception as
    ``ToolError(f"Error executing tool <name>: {original}")`` with no un-prefixed raise
    path, so the client-visible text carries that prefix ahead of this JSON payload. The
    ``error`` token is the SAME machine-parseable signal as before this gate (byte-identical
    to the prior bare-string message); only the *shape* becomes structured.
    """
    return ToolError(json.dumps({"error": error, "hint": hint}))


def _require_valid_id(entity_id: str) -> None:
    """Reject an injection-/malformed-shaped id BEFORE any query (logs to stderr, raises)."""
    if not read_guards.validate_entity_id(entity_id):
        # Log-on-rejection lands on stderr (never stdout); the raise surfaces as a
        # JSON-RPC error frame.
        logger.warning("rejected malformed entity id (failed ID_PATTERN): %r", entity_id)
        raise _tool_error(
            "invalid entity id",
            "must match the canonical id shape (ID_PATTERN); pass a resolved canonical id",
        )


# ----------------------------------------------------------------------------------------
# Thin, module-level tool functions — take the client explicitly so unit/property tests can
# drive them directly (no JSON-RPC loop). Each validates id-shape, clamps hops, and wraps
# the matching ``graph.queries`` helper verbatim (read-only).
# ----------------------------------------------------------------------------------------
def tool_get_entity(client: Neo4jClient, entity_id: str) -> dict[str, Any]:
    """Return a resolved entity's node properties (incl. its ``prov_*``); absent -> error."""
    _require_valid_id(entity_id)
    entity = queries.get_entity(client, entity_id=entity_id)
    if entity is None:
        raise _tool_error(
            "entity not found",
            "no resolved node has that id; verify the id or traverse from a known node",
        )
    return entity


def tool_get_neighbors(client: Neo4jClient, entity_id: str, hops: int = 1) -> list[dict[str, Any]]:
    """Return entities linked to ``entity_id`` within ``hops`` (clamped to the shared cap)."""
    _require_valid_id(entity_id)
    return queries.get_neighbors(client, entity_id=entity_id, hops=read_guards.clamp_hops(hops))


def tool_get_provenance(client: Neo4jClient, entity_id: str) -> dict[str, str]:
    """Return the node's ``prov_*`` map; absent -> error (parity with 2a's 404, ADR 0060)."""
    _require_valid_id(entity_id)
    prov = queries.get_provenance(client, entity_id=entity_id)
    if not prov:
        raise _tool_error(
            "entity not found",
            "the graph guarantees provenance on every present node, so absent means no node;"
            " verify the id or traverse from a known node",
        )
    return prov


def tool_find_paths(
    client: Neo4jClient, from_id: str, to_id: str, max_hops: int = 1
) -> list[dict[str, Any]]:
    """Return bounded paths between two entities (``max_hops`` clamped to the shared cap)."""
    _require_valid_id(from_id)
    _require_valid_id(to_id)
    return queries.find_paths(
        client, from_id=from_id, to_id=to_id, max_hops=read_guards.clamp_hops(max_hops)
    )


def _register_read_tools(server: FastMCP, client: Neo4jClient) -> None:
    """Register exactly the four read tools on ``server``, closing over ``client``.

    Shared by the stdio (:func:`build_server`) and HTTP (:func:`build_http_app`) transports so
    BOTH surfaces expose an identical 4-tool, read-only set — there is exactly one place a tool
    is registered (INV-S1-READONLY: the HTTP surface never drifts from stdio).
    """

    def get_entity(entity_id: str) -> dict[str, Any]:
        """Return a resolved entity's properties (incl. provenance); error if absent."""
        return tool_get_entity(client, entity_id)

    def get_neighbors(entity_id: str, hops: int = 1) -> list[dict[str, Any]]:
        """Return entities linked to an entity within ``hops`` (clamped to the cap)."""
        return tool_get_neighbors(client, entity_id, hops)

    def get_provenance(entity_id: str) -> dict[str, str]:
        """Return an entity's provenance (``prov_*``) map; error if absent."""
        return tool_get_provenance(client, entity_id)

    def find_paths(from_id: str, to_id: str, max_hops: int = 1) -> list[dict[str, Any]]:
        """Return bounded paths between two entities (``max_hops`` clamped to the cap)."""
        return tool_find_paths(client, from_id, to_id, max_hops)

    # Every tool below is read-only (calls only ``client.execute_read`` via the query
    # helpers), idempotent (a repeated read has no additional effect), and interacts with
    # a closed domain (our own resolved graph, never an open external world) — ADR 0121 D1.
    # ``destructiveHint`` is left unset (meaningful only when ``readOnlyHint == False``).
    # ``structured_output=True`` makes the SDK's existing schema derivation EXPLICIT and
    # fail-loud (ADR 0121 D2): it does not add net-new structured output — the SDK already
    # auto-detects it from these closures' return annotations — it just stops relying on
    # implicit auto-detection.
    read_only_annotations = ToolAnnotations(
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
    server.add_tool(
        get_entity,
        name="get_entity",
        title="Get entity",
        annotations=read_only_annotations,
        structured_output=True,
    )
    server.add_tool(
        get_neighbors,
        name="get_neighbors",
        title="Get neighbors",
        annotations=read_only_annotations,
        structured_output=True,
    )
    server.add_tool(
        get_provenance,
        name="get_provenance",
        title="Get provenance",
        annotations=read_only_annotations,
        structured_output=True,
    )
    server.add_tool(
        find_paths,
        name="find_paths",
        title="Find paths",
        annotations=read_only_annotations,
        structured_output=True,
    )


def _resolve_client(neo4j_client: Neo4jClient | None) -> Neo4jClient:
    """Use an injected client verbatim (no connection opened); else build from settings."""
    return neo4j_client if neo4j_client is not None else Neo4jClient.from_settings()


def build_server(*, neo4j_client: Neo4jClient | None = None) -> FastMCP:
    """Build the **stdio** FastMCP server, registering exactly the four read tools (ADR 0063).

    The Neo4j client is INJECTABLE for testability (mirrors ``create_app``, ADR 0062):
    an injected client is used verbatim and NO connection is opened here; only when
    ``neo4j_client is None`` is the default ``Neo4jClient.from_settings()`` constructed.

    This is the unchanged stdio path: NO auth, NO port, NO token verifier (INV-S1-STDIO).
    """
    configure_stderr_logging()
    server: FastMCP = FastMCP(name="worldmonitor-graph-read")
    _register_read_tools(server, _resolve_client(neo4j_client))
    return server


def build_http_app(
    *,
    neo4j_client: Neo4jClient | None = None,
    token_verifier: ZitadelMCPTokenVerifier,
) -> Any:
    """Build the **authenticated** ``streamable-http`` ASGI app for a remote Hermes (ADR 0090).

    ``token_verifier`` is REQUIRED and has no default: omitting it raises ``TypeError`` before
    any code runs, so an anonymous HTTP MCP port can never be constructed (INV-S1-AUTH,
    fail-closed). The verifier (a :class:`ZitadelMCPTokenVerifier`) is wired through the SDK's
    native ``BearerAuthBackend`` + ``RequireAuthMiddleware`` via ``FastMCP(auth=…,
    token_verifier=…)``; we hand-roll no middleware.

    Exposes EXACTLY the same four read tools as :func:`build_server` (INV-S1-READONLY). The
    issuer/resource come from settings; in an unconfigured dev/test environment (no
    ``zitadel_domain``) the issuer falls back to a localhost placeholder so the app can still
    be constructed — real token validation is done by the wrapped Zitadel verifier regardless.

    Unlike the stdio path, this transport does NOT call ``configure_stderr_logging()``: STDOUT
    PURITY is a stdio-only invariant (there, stdout is the JSON-RPC frame channel). Over HTTP the
    frames travel in the response body, so logging may use the process default — and forcing the
    process-global stderr handler here would otherwise fight stdio logging config in-process.
    """
    settings = get_settings()

    issuer_url = settings.oidc_issuer or _DEV_FALLBACK_ISSUER
    if not settings.oidc_issuer:
        logger.warning(
            "no zitadel_domain configured; using a localhost issuer placeholder for MCP "
            "protected-resource metadata (token validation still uses the wrapped verifier)"
        )
    resource_server_url = settings.mcp_resource_server_url or None

    auth_settings = build_auth_settings(
        issuer_url=issuer_url, resource_server_url=resource_server_url
    )
    server: FastMCP = FastMCP(
        name="worldmonitor-graph-read",
        auth=auth_settings,
        token_verifier=token_verifier,
        # Stateless HTTP (no server-side session continuity / no mcp-session-id requirement):
        # the read-only tool surface holds no per-session state, and statelessness keeps the
        # transport 12-factor (any replica serves any request, no sticky sessions).
        stateless_http=True,
    )
    _register_read_tools(server, _resolve_client(neo4j_client))
    return server.streamable_http_app()


# Localhost issuer used ONLY when zitadel_domain is unset (dev/test). Production always sets
# zitadel_domain, so oidc_issuer is the real issuer and this is never reached.
_DEV_FALLBACK_ISSUER = "http://localhost"


def main() -> None:
    """Console entrypoint — run the MCP server on the configured transport.

    Default is stdio (ADR 0063, unchanged). When ``mcp_transport == "streamable-http"`` the
    server runs the authenticated HTTP transport (ADR 0090): a ``ZitadelTokenVerifier`` is built
    from settings, wrapped by :class:`ZitadelMCPTokenVerifier`, and the HTTP app is served on the
    configured host/port. Fail-closed: HTTP requires the verifier, which requires a configured
    issuer/JWKS/audience.
    """
    configure_stderr_logging()
    settings = get_settings()

    if settings.mcp_transport == "streamable-http":
        import uvicorn

        # Fail-closed AND loud: refuse to start the HTTP transport unless Zitadel is configured.
        # Without it the wrapped verifier would reject every token (a server that 401s everything),
        # which is safe but a silent operator footgun — so we hard-fail at boot with a clear cause
        # instead of serving a uselessly-locked port.
        if not settings.zitadel_domain:
            raise RuntimeError(
                "mcp_transport=streamable-http requires zitadel_domain (+ client_id) to be "
                "configured: the HTTP MCP transport is bearer-gated and cannot verify tokens "
                "without an OIDC issuer/JWKS. Set ZITADEL_DOMAIN or run the stdio transport."
            )

        verifier = ZitadelMCPTokenVerifier(
            ZitadelTokenVerifier(
                issuer=settings.oidc_issuer,
                jwks_uri=settings.oidc_jwks_uri,
                audience=settings.zitadel_client_id,
            )
        )
        app = build_http_app(token_verifier=verifier)
        uvicorn.run(app, host=settings.mcp_http_host, port=settings.mcp_http_port)
    else:
        build_server().run(transport="stdio")
