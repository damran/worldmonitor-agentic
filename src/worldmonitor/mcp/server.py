"""Graph-read FastMCP **stdio** server (ADR 0063, slice 2b; Gate F-3, ADR 0122).

The MCP twin of slice 2a's REST routes: it re-exposes the resolved graph over a
FastMCP stdio server with exactly five structured, read-only, bounded, parameterized
tools — ``get_entity`` / ``get_neighbors`` / ``get_provenance`` / ``find_paths`` /
``get_entity_dossier`` — each wrapping the SAME ``graph/queries.py`` helper and the
SAME shared ``graph/read_guards`` (hop clamp + id validation) as REST. There is **no**
raw-Cypher tool (same call as ADR 0062).

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
from mcp.server.fastmcp.prompts.base import Prompt
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


# ==========================================================================================
# Analyst-playbook prompts (Gate F-4, ADR 0125) — declarative, read-only MCP prompts. A
# prompt is a pure text template parameterised only by a validated, canonical-id-shaped
# argument: it opens no session, issues no Cypher, and returns no graph node/edge (spec §0,
# §3 "Append-only / read-only"). Registered ONCE (``_register_prompts``) and called by BOTH
# transports so stdio and HTTP never drift (D2, INV-S1).
# ==========================================================================================

# Generous versus any real canonical id (Wikidata Q-numbers, LEI, GeoNames, ISO-3166,
# `opensanctions:…`, IOC-feed ids…) while clearly rejecting a hostile blob (spec §5.1).
_PROMPT_ARG_MAX_LEN = 256

# VERBATIM contract text (spec §4.1) — pinned by a byte-identical test. `{{count, sample}}`
# is the doubled-brace escape: it renders as the literal `{count, sample}` after
# `.format()`; the only real substitution field is `{entity_id}`.
_ENTITY_WORKUP_TEMPLATE = """Entity workup playbook — entity_id={entity_id}

Purpose: assemble a provenance-complete picture of one resolved entity as ranked leads
for a human analyst. This is a read-only orientation aid, never an automated verdict.

Each step names one graph-read tool, its purpose, and the argument shape to pass. Work
through them in order.

Step 1 - Anchor the entity.
  Tool: get_entity(entity_id={entity_id})
  Purpose: confirm the node exists and read its properties and provenance
  (prov_source_id, prov_retrieved_at, prov_reliability, prov_source_record). If the tool
  reports "entity not found", stop: this id is not in the resolved graph.

Step 2 - Read the provenance.
  Tool: get_provenance(entity_id={entity_id})
  Purpose: record where each fact about this entity came from before drawing any
  inference. Every node carries provenance; weigh a single-source or low-reliability fact
  as a weaker lead.

Step 3 - Map the immediate neighbourhood.
  Tool: get_neighbors(entity_id={entity_id}, hops=1)
  Purpose: list the entities one edge away and how they connect. On a high-degree node,
  pass summary=true first for a {{count, sample}} taste before requesting the full list.

Step 4 - Trace a specific connection.
  Tool: find_paths(from_id={entity_id}, to_id=<a second resolved id>, max_hops=<within
  the hop cap>)
  Purpose: when you have a hypothesis linking this entity to another, look for bounded
  paths between them. No path within the hop bound is not proof of no relationship.

Step 5 - Assemble the dossier.
  Tool: get_entity_dossier(entity_id={entity_id}, hops=1)
  Purpose: retrieve the deterministic entity + neighbours + provenance + merge_history
  bundle in a single call for the written workup.

Framing: report ranked hypotheses with their provenance and confidence for human review;
do not merge, attribute, or label a person from this workup. Surface the leads and their
sources and let a human decide."""

# VERBATIM contract text (spec §4.2) — pinned by a byte-identical test. `{scope_line}` is
# computed by :func:`prompt_freshness_audit` from the validated ``connector_id``.
_FRESHNESS_AUDIT_TEMPLATE = """Source freshness audit playbook

{scope_line}

Purpose: assess how current the ingested sources are, so an analyst can weight findings by
the freshness of their underlying feeds. A stale or missing source is a lead about a
collection gap, not a verdict about the world.

Freshness is served by the read-only REST endpoint GET /sources/freshness (auth-gated,
single-tenant). There is no freshness MCP tool in this version - use the REST surface. The
response lists, per connector instance: connector_id, an opaque instance_id, the raw
status, the derived freshness_status, last_success_at, age_seconds, plus a summary
count-by-state and the configured staleness budget.

Step 1 - Pull the freshness surface.
  Call: GET /sources/freshness
  Purpose: read the current per-instance freshness_status and the summary counts.

Step 2 - Read each instance's freshness_status. It is one of six values, in priority
order:
  - disabled    administratively off; expect no data, not a fault.
  - error       auto-hard-disabled after repeated failures; a collection gap to escalate
                to a human operator.
  - no_data     active but never had a successful ingest; treat downstream coverage as
                absent.
  - very_stale  last success older than the very-stale budget; findings may be badly out
                of date.
  - stale       last success older than the stale budget; weight findings accordingly.
  - fresh       last success within budget.

Step 3 - Summarise the gaps.
  Purpose: from the summary counts, report how many instances are error / no_data /
  very_stale / stale versus fresh. Rank error and no_data instances first - those are the
  collection gaps most likely to bias an analysis.

Framing: freshness is operational metadata about pipelines, never data about a person.
Read it as evidence of collection coverage and gaps and surface those gaps to a human; do
not treat a stale or missing source as a factual claim about any entity."""


def _prompt_error(error: str, hint: str) -> ValueError:
    """Build a ``ValueError`` whose message is a JSON ``{"error", "hint"}`` envelope.

    Mirrors :func:`_tool_error`, but a bare ``ValueError`` (not ``ToolError``) because that
    is what the SDK's ``Prompt.render``/``FastMCP.get_prompt`` re-raise regardless of what a
    prompt fn raises (spec §1.1): the client-visible text carries the SDK's own
    ``Error rendering prompt <name>: `` prefix ahead of this JSON payload.
    """
    return ValueError(json.dumps({"error": error, "hint": hint}))


def _require_valid_prompt_arg(value: str, *, field: str, allow_empty: bool = False) -> None:
    """Reject an over-length or malformed-shaped prompt argument BEFORE interpolation.

    Validation order is load-bearing (spec §5.1): length is checked FIRST — an over-cap
    string is rejected as "argument too long" even when it would also fail shape
    validation, and checking length first avoids running the shape regex against an
    adversarial giant string. Shape is checked SECOND via the SAME
    ``read_guards.validate_entity_id`` predicate the five tools use (ADR 0125 D3). Neither
    branch logs or raises the raw value unbounded — only ``field``, its length, and a short
    ``repr`` slice reach the (stderr-only) log line; the error envelope always carries a
    fixed token + hint and never reflects the hostile bytes.
    """
    if allow_empty and value == "":
        return
    if len(value) > _PROMPT_ARG_MAX_LEN:
        logger.warning(
            "rejected over-length prompt argument %s (len=%d, starts=%r)",
            field,
            len(value),
            value[:32],
        )
        raise _prompt_error(
            "argument too long",
            f"{field} must be at most {_PROMPT_ARG_MAX_LEN} characters",
        )
    if not read_guards.validate_entity_id(value):
        logger.warning(
            "rejected malformed prompt argument %s (failed ID_PATTERN, len=%d, starts=%r)",
            field,
            len(value),
            value[:32],
        )
        raise _prompt_error(
            "invalid argument",
            f"{field} must match the canonical id shape (ID_PATTERN)",
        )


def prompt_entity_workup(entity_id: str) -> str:
    """Render the ``entity-workup`` playbook for one validated canonical entity id.

    ``entity_id`` is length-capped then shape-validated (spec §5.1, required/non-empty);
    on success this returns the verbatim template (spec §4.1) with ``entity_id``
    interpolated. Pure text: no session, no Cypher, no client (spec §0).
    """
    _require_valid_prompt_arg(entity_id, field="entity_id")
    return _ENTITY_WORKUP_TEMPLATE.format(entity_id=entity_id)


def prompt_freshness_audit(connector_id: str = "") -> str:
    """Render the ``freshness-audit`` playbook, optionally scoped to one connector id.

    ``connector_id == ""`` (the default) is the "all instances" sentinel and skips shape
    validation (spec §5.1); a non-empty value is length-capped then shape-validated
    identically to ``entity_id``. Pure text: no session, no Cypher, no client (spec §0).
    """
    _require_valid_prompt_arg(connector_id, field="connector_id", allow_empty=True)
    if connector_id == "":
        scope_line = "Scope: all connector instances."
    else:
        scope_line = f"Scope: focus on connector instances whose connector_id == {connector_id}."
    return _FRESHNESS_AUDIT_TEMPLATE.format(scope_line=scope_line)


def _register_prompts(server: FastMCP) -> None:
    """Register the two analyst-playbook prompts on ``server`` (Gate F-4, ADR 0125 D2).

    Shared by the stdio (:func:`build_server`) and HTTP (:func:`build_http_app`) transports
    so BOTH surfaces expose an identical two-prompt set with no drift. Takes **no**
    ``Neo4jClient`` — prompts are pure text (spec §0, §3): they open no session and issue no
    Cypher. Hyphenated wire names require the explicit ``name=`` kwarg (spec §1).
    """
    server.add_prompt(
        Prompt.from_function(
            prompt_entity_workup,
            name="entity-workup",
            title="Entity workup",
            description=(
                "Declarative, read-only playbook: work up a single resolved entity across "
                "the five graph-read tools (get_entity → get_provenance → get_neighbors → "
                "find_paths → get_entity_dossier) as ranked leads for human review — never "
                "an automated verdict."
            ),
        )
    )
    server.add_prompt(
        Prompt.from_function(
            prompt_freshness_audit,
            name="freshness-audit",
            title="Freshness audit",
            description=(
                "Declarative, read-only playbook: audit source freshness via GET "
                "/sources/freshness and interpret the six freshness states as evidence of "
                "collection gaps for human review."
            ),
        )
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


def tool_get_neighbors(
    client: Neo4jClient, entity_id: str, hops: int = 1, summary: bool = False
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return entities linked to ``entity_id`` within ``hops`` (clamped to the shared cap).

    ``summary=True`` (Gate F-5, ADR 0124) returns the shared context-budget envelope
    ``{count, sample}`` (:func:`worldmonitor.graph.queries.summarize_result`) in place of the
    full list; ``summary`` absent/false is byte-identical to before this gate.
    """
    _require_valid_id(entity_id)
    neighbors = queries.get_neighbors(
        client, entity_id=entity_id, hops=read_guards.clamp_hops(hops)
    )
    if summary:
        return queries.summarize_result(neighbors)
    return neighbors


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
    client: Neo4jClient, from_id: str, to_id: str, max_hops: int = 1, summary: bool = False
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return bounded paths between two entities (``max_hops`` clamped to the shared cap).

    ``summary=True`` (Gate F-5, ADR 0124) returns the shared context-budget envelope
    ``{count, sample}`` (:func:`worldmonitor.graph.queries.summarize_result`) in place of the
    full list; ``summary`` absent/false is byte-identical to before this gate.
    """
    _require_valid_id(from_id)
    _require_valid_id(to_id)
    paths = queries.find_paths(
        client, from_id=from_id, to_id=to_id, max_hops=read_guards.clamp_hops(max_hops)
    )
    if summary:
        return queries.summarize_result(paths)
    return paths


def tool_get_entity_dossier(client: Neo4jClient, entity_id: str, hops: int = 1) -> dict[str, Any]:
    """Return the deterministic entity dossier (Gate F-3 slice 1, ADR 0122); absent -> error.

    A thin pass-through of :func:`worldmonitor.graph.queries.get_entity_dossier` — the SAME
    shared assembly helper the REST route calls, so the two surfaces never drift (ADR 0122
    D1). ``hops`` is clamped to the shared cap before reaching the helper, mirroring
    :func:`tool_get_neighbors`; the ``"entity not found"`` error token is REUSED verbatim
    from :func:`tool_get_entity` so the not-found signal stays consistent across tools.
    """
    _require_valid_id(entity_id)
    dossier = queries.get_entity_dossier(
        client, entity_id=entity_id, hops=read_guards.clamp_hops(hops)
    )
    if dossier is None:
        raise _tool_error(
            "entity not found",
            "no resolved node has that id; verify the id or traverse from a known node",
        )
    return dossier


def _register_read_tools(server: FastMCP, client: Neo4jClient) -> None:
    """Register exactly the five read tools on ``server``, closing over ``client``.

    Shared by the stdio (:func:`build_server`) and HTTP (:func:`build_http_app`) transports so
    BOTH surfaces expose an identical 5-tool, read-only set — there is exactly one place a tool
    is registered (INV-S1-READONLY: the HTTP surface never drifts from stdio).
    """

    def get_entity(entity_id: str) -> dict[str, Any]:
        """Return a resolved entity's properties (incl. provenance); error if absent."""
        return tool_get_entity(client, entity_id)

    def get_neighbors(
        entity_id: str, hops: int = 1, summary: bool = False
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return entities linked to an entity within ``hops`` (clamped to the cap).

        ``summary=True`` (Gate F-5, ADR 0124) returns ``{count, sample}`` in place of the
        full list; ``summary`` absent/false is byte-identical to before this gate.
        """
        return tool_get_neighbors(client, entity_id, hops, summary)

    def get_provenance(entity_id: str) -> dict[str, str]:
        """Return an entity's provenance (``prov_*``) map; error if absent."""
        return tool_get_provenance(client, entity_id)

    def find_paths(
        from_id: str, to_id: str, max_hops: int = 1, summary: bool = False
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return bounded paths between two entities (``max_hops`` clamped to the cap).

        ``summary=True`` (Gate F-5, ADR 0124) returns ``{count, sample}`` in place of the
        full list; ``summary`` absent/false is byte-identical to before this gate.
        """
        return tool_find_paths(client, from_id, to_id, max_hops, summary)

    def get_entity_dossier(entity_id: str, hops: int = 1) -> dict[str, Any]:
        """Return the deterministic entity dossier (entity + neighbors + provenance +
        merge_history); error if the entity is absent (Gate F-3, ADR 0122)."""
        return tool_get_entity_dossier(client, entity_id, hops)

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
    server.add_tool(
        get_entity_dossier,
        name="get_entity_dossier",
        title="Get entity dossier",
        annotations=read_only_annotations,
        structured_output=True,
    )


def _resolve_client(neo4j_client: Neo4jClient | None) -> Neo4jClient:
    """Use an injected client verbatim (no connection opened); else build from settings."""
    return neo4j_client if neo4j_client is not None else Neo4jClient.from_settings()


def build_server(*, neo4j_client: Neo4jClient | None = None) -> FastMCP:
    """Build the **stdio** FastMCP server, registering exactly the five read tools (ADR 0063,
    ADR 0122).

    The Neo4j client is INJECTABLE for testability (mirrors ``create_app``, ADR 0062):
    an injected client is used verbatim and NO connection is opened here; only when
    ``neo4j_client is None`` is the default ``Neo4jClient.from_settings()`` constructed.

    This is the unchanged stdio path: NO auth, NO port, NO token verifier (INV-S1-STDIO).
    """
    configure_stderr_logging()
    server: FastMCP = FastMCP(name="worldmonitor-graph-read")
    _register_read_tools(server, _resolve_client(neo4j_client))
    _register_prompts(server)
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

    Exposes EXACTLY the same five read tools as :func:`build_server` (INV-S1-READONLY). The
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
    _register_prompts(server)
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
